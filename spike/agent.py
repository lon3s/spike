"""Agenten-Schleife: Modell aufrufen → Werkzeuge ausführen → wiederholen. Läuft im Hintergrund-Thread."""
import json
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import memory
from .client import complete, stream_chat
from .context import budget, build_messages, estimate_tokens, pick_cut, summary_request
from .tools import MEMORY_TOOLS, expand, specs_for

NO_THINK_SWITCH = set()     # Server-URLs, die chat_template_kwargs ablehnen (nur für diese Sitzung)


def list_first(gen):
    """Startet den Stream (damit HTTP-Fehler sofort kommen) und liefert ihn danach vollständig weiter."""
    import itertools
    try:
        first = next(gen)
    except StopIteration:
        return iter(())
    return itertools.chain([first], gen)


MEMORY_HOWTO = (
    "Wenn der Nutzer dir etwas Dauerhaftes über sich, seine Vorlieben oder seine Systeme mitteilt oder "
    "„merk dir …“ sagt, speichere es knapp mit memory_save. Fehlt dir Wissen über den Nutzer, suche mit "
    "memory_search. Erwähne das Gedächtnis nur, wenn es passt."
)


class Agent(QObject):
    changed = Signal()
    stats = Signal(str, int, int)        # text, genutzte tokens, n_ctx
    usage = Signal(int, int, float)      # genutzte tokens, n_ctx, tok/s (-1 = Schätzung, noch keine Messung)
    speed = Signal(float)                # tok/s live während des Streamens
    error = Signal(str)
    info = Signal(str)
    confirm_request = Signal(str)
    finished = Signal()

    def __init__(self, chat, endpoint, model, profile, settings, toolbox, n_ctx):
        super().__init__()
        self.chat, self.ep, self.model, self.profile = chat, endpoint, model, profile
        self.settings, self.toolbox, self.n_ctx = settings, toolbox, n_ctx
        self.live = None          # {"content", "reasoning"} während des Streamens
        self.stopped = False
        self._ev = threading.Event()
        self._answer = False
        self._last_emit = 0.0
        self.pending = None       # offene Freigabe: {"text", "key", "label"} – wird im Chat als Karte gezeigt
        self.live_tool = None     # laufendes Werkzeug: {"id", "out"} – Live-Ausgabe im Terminal-Block
        self._cur = ("", {})
        self.vision = False       # versteht der Server Bilder? (setzt die Oberfläche)
        self.auto_deny = False    # geplante Aufgaben: niemand da zum Bestätigen → automatisch ablehnen
        self._remember = False
        self._reason = ""

    # --- Steuerung ---
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def start_compact(self, hint=""):
        """Nur den bisherigen Verlauf zusammenfassen (Befehl /compact), ohne neue Anfrage."""
        def job():
            try:
                max_tokens = int(self.profile.get("max_tokens") or 0)
                if not self._compact(max_tokens, force=True, hint=hint):
                    self.info.emit("Nichts zum Zusammenfassen – der Verlauf ist noch kurz.")
                self.estimate()
            except Exception as e:
                self.error.emit(f"{type(e).__name__}: {e}")
            finally:
                self.changed.emit()
                self.finished.emit()
        threading.Thread(target=job, daemon=True).start()

    def estimate(self) -> int:
        """Geschätzte Kontext-Belegung der nächsten Anfrage (inkl. Werkzeug-Beschreibungen)."""
        s = self.settings
        payload, _ = build_messages(
            self._system_prompt(), self.chat["messages"], 0, 0,
            int(s.get("keep_full_tool_results", 2)), int(s.get("old_tool_result_chars", 300)),
            self.chat.get("summary", ""), int(self.chat.get("summary_upto", 0)), self.vision)
        tools = specs_for(self.profile.get("tools", []))
        est = estimate_tokens(payload) + (estimate_tokens([{"t": json.dumps(tools)}]) if tools else 0)
        self.usage.emit(est, self.n_ctx or 0, -1.0)
        return est

    def stop(self):
        self.stopped = True
        self._answer = False
        self._ev.set()
        self.toolbox.kill()

    def answer(self, ok: bool, remember=False, reason=""):
        self._answer, self._remember, self._reason = ok, remember, reason
        self._ev.set()

    # Was „Immer erlauben“ freischaltet: bei Shell-Befehlen nur dasselbe Programm, sonst das Werkzeug
    def _allow_key(self):
        name, args = self._cur
        if name == "run_shell":
            words = [w for w in str(args.get("command", "")).split() if w not in ("sudo", "env")]
            return f"run_shell:{words[0]}" if words else ""
        if name == "pc":
            return f"pc:{args.get('action', '')}"
        return name

    @staticmethod
    def allow_label(key):
        if key.startswith("run_shell:"):
            return f"Befehle mit „{key.split(':', 1)[1]}“"
        if key.startswith("pc:"):
            return {"pc:open": "Öffnen von Dateien/Programmen"}.get(key, key)
        return {"write_file": "Dateien schreiben", "edit_file": "Dateien ändern",
                "python_run": "Python ausführen"}.get(key, key)

    def _confirm(self, text, diff=None) -> bool:
        if self.stopped:
            return False
        key = self._allow_key()
        if key and key in self.chat.get("allow", []):
            return True
        if self.auto_deny:
            self._reason = ("Geplante Aufgabe – der Nutzer ist nicht da und kann nichts freigeben. Erledige es "
                            "ohne diese Aktion oder beschreibe, was er selbst tun soll.")
            return False
        short = (f"„{key.split(':', 1)[1]}“" if key.startswith("run_shell:") else
                 {"pc:open": "Öffnen", "write_file": "Dateien schreiben", "edit_file": "Dateien ändern",
                  "python_run": "Python"}.get(key, key[:18]))
        self.pending = {"text": text, "key": key, "label": self.allow_label(key) if key else "", "short": short,
                        "diff": diff or ""}
        self._ev.clear()
        self._tick(force=True)
        self.confirm_request.emit(text)
        self._ev.wait()
        self.pending = None
        ok = self._answer and not self.stopped
        if ok and self._remember and key:
            self.chat.setdefault("allow", []).append(key)
        self._tick(force=True)
        return ok

    def _tick(self, force=False):
        now = time.monotonic()
        if force or now - self._last_emit > 0.06:
            self._last_emit = now
            self.changed.emit()

    # --- System-Prompt mit Gedächtnis-Auszug ---
    def _system_prompt(self):
        system = (self.profile.get("system_prompt") or "").replace("{date}", time.strftime("%d.%m.%Y"))
        s = self.settings
        if not s.get("memory_enabled", True):
            return system
        # Auszug einmal pro Chat bilden und dann gleich lassen (Prompt-Cache des Servers bleibt gültig)
        mem = self.chat.get("memory")
        if mem is None:
            first = next((m.get("content") or "" for m in self.chat["messages"] if m["role"] == "user"), None)
            text, n, total = memory.snapshot(first or "", int(s.get("memory_prompt_chars", 1500)))
            mem = {"text": text, "n": n, "total": total}
            if first is not None:     # erst festschreiben, wenn die erste Nachricht da ist
                self.chat["memory"] = mem
        has_tools = any(t in expand(self.profile.get("tools", [])) for t in MEMORY_TOOLS)
        block = ""
        if mem.get("text"):
            more = " – weitere mit memory_search" if has_tools and mem["total"] > mem["n"] else ""
            block = f"## Was du über den Nutzer weißt ({mem['n']} von {mem['total']} Einträgen{more})\n{mem['text']}"
        if has_tools:
            block = (block + "\n\n" if block else "") + MEMORY_HOWTO
        return (system + "\n\n" + block).strip() if block else system

    # --- Verlauf zusammenfassen, wenn der Kontext voll wird ---
    def _compact(self, max_tokens, force=False, hint="") -> bool:
        msgs, s = self.chat["messages"], self.settings
        start = int(self.chat.get("summary_upto", 0))
        keep = int(budget(self.n_ctx, max_tokens) * int(s.get("compact_keep_percent", 30)) / 100)
        if force:   # manuell: höchstens die letzte Runde wörtlich behalten
            keep = min(keep, max(1, estimate_tokens(msgs[start:]) // 4))
        cut = pick_cut(msgs, start, keep)
        if cut is None:
            return False
        last_user = next((m.get("content") or "" for m in reversed(msgs[:cut]) if m["role"] == "user"), "")
        if any(m["role"] == "user" for m in msgs[cut:]):
            last_user = ""        # die aktuelle Aufgabe steht ohnehin noch wörtlich im Verlauf
        self.info.emit(f"📦 Kontext wird voll – fasse {cut - start} ältere Nachrichten zusammen …")
        # Zusammenfassungs-Anfrage selbst darf höchstens ~60 % des Kontexts belegen
        max_chars = int(self.n_ctx * 0.6 * 3.3)
        req = summary_request(self.chat.get("summary", ""), msgs, start, cut, last_user, max_chars)
        if hint:
            req[-1]["content"] += f"\n\nBesonderer Wunsch des Nutzers für die Zusammenfassung: {hint}"
        try:
            summary = complete(self.ep, self.model, req, {"temperature": 0.2, "max_tokens": 2000},
                               lambda: self.stopped)
        except Exception as e:
            self.info.emit(f"⚠ Zusammenfassen fehlgeschlagen ({e}) – ältere Nachrichten werden weggelassen.")
            return False
        if not summary or self.stopped:
            return False
        self.chat["summary"] = summary
        self.chat["summary_upto"] = cut
        self.chat.setdefault("compactions", []).append({"at": cut, "time": time.time()})
        self.info.emit(f"📦 {cut - start} ältere Nachrichten zusammengefasst ({len(summary)} Zeichen).")
        return True

    # --- Hauptschleife ---
    def _run(self):
        try:
            self._loop()
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.live = None
            self.changed.emit()
            self.finished.emit()

    def _loop(self):
        msgs = self.chat["messages"]
        s = self.settings
        tools = specs_for(self.profile.get("tools", []))
        system = self._system_prompt()
        max_tokens = int(self.profile.get("max_tokens") or 0)
        params = {"temperature": float(self.profile.get("temperature", 0.7))}
        thinking = self.profile.get("thinking", "standard")
        if thinking in ("an", "aus") and self.ep.get("url") not in NO_THINK_SWITCH:
            # llama.cpp + Qwen & Co.: Denken pro Anfrage an/aus
            params["chat_template_kwargs"] = {"enable_thinking": thinking == "an"}
        if max_tokens:
            params["max_tokens"] = max_tokens

        def payload_now():
            return build_messages(
                system, msgs, self.n_ctx, max_tokens,
                int(s.get("keep_full_tool_results", 2)), int(s.get("old_tool_result_chars", 300)),
                self.chat.get("summary", ""), int(self.chat.get("summary_upto", 0)), self.vision)

        for step in range(int(s.get("max_steps", 15))):
            payload, dropped = payload_now()
            if self.n_ctx and s.get("auto_compact", True) and not self.stopped:
                limit = budget(self.n_ctx, max_tokens) * int(s.get("compact_at_percent", 70)) / 100
                if estimate_tokens(payload) + estimate_tokens([{"t": json.dumps(tools)}]) > limit:
                    if self._compact(max_tokens):
                        payload, dropped = payload_now()
            if dropped:
                self.info.emit(f"{dropped} alte Nachrichten passen nicht mehr in den Kontext und wurden ausgelassen.")
            est = estimate_tokens(payload)
            self.stats.emit(f"~{est} Tokens (geschätzt)", est, self.n_ctx or 0)

            self.live = {"content": "", "reasoning": ""}
            self.usage.emit(estimate_tokens(payload), self.n_ctx or 0, -1.0)
            calls, usage, timings = {}, None, None
            n_tok, t_first, t_emit = 0, None, 0.0
            finish = None
            try:
                stream = list_first(stream_chat(self.ep, self.model, payload, tools, params, lambda: self.stopped))
            except RuntimeError as e:
                if "chat_template_kwargs" in params and "HTTP 4" in str(e):
                    params.pop("chat_template_kwargs")      # Server kennt den Schalter nicht → ohne
                    NO_THINK_SWITCH.add(self.ep.get("url"))
                    self.info.emit("Hinweis: Dieser Server kann Denken nicht pro Anfrage schalten.")
                    stream = list_first(stream_chat(self.ep, self.model, payload, tools, params, lambda: self.stopped))
                else:
                    raise
            for chunk in stream:
                usage = chunk.get("usage") or usage
                timings = chunk.get("timings") or timings
                if chunk.get("error"):      # manche Server schicken Fehler mitten im Stream
                    err = chunk["error"]
                    raise RuntimeError(f"Server-Fehler: {err.get('message', err) if isinstance(err, dict) else err}")
                for ch in chunk.get("choices") or []:
                    finish = ch.get("finish_reason") or finish
                    d = ch.get("delta") or {}
                    if d:     # grob: ein Stream-Häppchen ≈ ein Token
                        n_tok += 1
                        now = time.monotonic()
                        t_first = t_first or now
                        if now - t_emit > 0.5 and now > t_first:
                            t_emit = now
                            self.speed.emit(n_tok / (now - t_first))
                    if d.get("reasoning_content"):
                        self.live["reasoning"] += d["reasoning_content"]
                    if d.get("content"):
                        self.live["content"] += d["content"]
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        f = tc.get("function") or {}
                        slot["name"] += f.get("name") or ""
                        slot["arguments"] += f.get("arguments") or ""
                self._tick()

            if usage:
                used = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
                txt = f"{used} Tokens"
                tps = float((timings or {}).get("predicted_per_second") or 0)
                if tps:
                    txt += f" · {tps:.1f} tok/s"
                self.stats.emit(txt, used, self.n_ctx or 0)
                self.usage.emit(used, self.n_ctx or 0, tps)

            if not finish and not self.stopped and not calls:
                if not self.live["content"]:
                    raise RuntimeError("Der Server hat die Verbindung ohne Antwort beendet – vermutlich abgestürzt "
                                       "(z. B. beim Verarbeiten eines Bildes). Bitte das Server-Log prüfen.")
                self.info.emit("⚠ Antwort wurde vom Server abgebrochen (unvollständig).")
            msg = {"role": "assistant", "content": self.live["content"]}
            if self.live["reasoning"]:
                msg["_reasoning"] = self.live["reasoning"]   # nur Anzeige, wird nicht zurückgeschickt
            tool_calls = [
                {"id": c["id"] or f"call_{step}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in sorted(calls.items()) if c["name"]
            ]
            if tool_calls and not self.stopped:
                msg["tool_calls"] = tool_calls
            if msg["content"] or tool_calls:
                msgs.append(msg)
            self.live = {"content": "", "reasoning": ""}
            self._tick(force=True)

            if self.stopped or not msg.get("tool_calls"):
                return

            pending_images = []
            for c in tool_calls:
                name = c["function"]["name"]
                if self.stopped:
                    result = "Abgebrochen."
                else:
                    try:
                        args = json.loads(c["function"]["arguments"] or "{}")
                        if not isinstance(args, dict):
                            raise ValueError
                        self.info.emit(f"🔧 {name} läuft …")
                        self._cur, self._reason = (name, args), ""
                        self.toolbox.last_diff = ""
                        self.toolbox.last_undo = None
                        self.live_tool = {"id": c["id"], "out": ""}

                        def on_out(text, lt=self.live_tool):
                            lt["out"] = (lt["out"] + text)[-20000:]
                            self._tick()
                        self.toolbox.on_output = on_out
                        try:
                            result = self.toolbox.run(name, args, self._confirm)
                        finally:
                            self.toolbox.on_output = None
                            self.live_tool = None
                        if self._reason and result.startswith("Vom Nutzer abgelehnt"):
                            result = f"Vom Nutzer abgelehnt. Er schreibt dazu: {self._reason}"
                    except (json.JSONDecodeError, ValueError):
                        result = "Fehler: Argumente sind kein gültiges JSON-Objekt."
                tmsg = {"role": "tool", "tool_call_id": c["id"], "name": name, "content": result}
                if self.toolbox.last_diff:
                    tmsg["_diff"] = self.toolbox.last_diff      # nur Anzeige
                    self.toolbox.last_diff = ""
                if self.toolbox.last_undo:
                    tmsg["_undo"] = self.toolbox.last_undo      # für ↶ Rückgängig / /undo
                    self.toolbox.last_undo = None
                msgs.append(tmsg)
                for att in self.toolbox.take_images():          # z. B. Screenshot-Werkzeug
                    pending_images.append(att)
                self._tick(force=True)
            if pending_images:   # Bilder erst nach allen Werkzeug-Antworten (Reihenfolge der Rollen bleibt gültig)
                msgs.append({"role": "user", "content": "[Bild vom Werkzeug, z. B. Screenshot]", "_auto": True,
                             "_images": [a["path"] for a in pending_images], "_attachments": pending_images})
                self._tick(force=True)
            self.info.emit("")

        self.info.emit("Maximale Anzahl an Werkzeug-Schritten erreicht.")
