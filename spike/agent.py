"""Agenten-Schleife: Modell aufrufen → Werkzeuge ausführen → wiederholen. Läuft im Hintergrund-Thread."""
import json
import threading
import time

from PySide6.QtCore import QObject, Signal

from .client import stream_chat
from .context import build_messages, estimate_tokens
from .tools import specs_for


class Agent(QObject):
    changed = Signal()
    stats = Signal(str, int, int)        # text, genutzte tokens, n_ctx
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

    # --- Steuerung ---
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stopped = True
        self._answer = False
        self._ev.set()

    def answer(self, ok: bool):
        self._answer = ok
        self._ev.set()

    def _confirm(self, text) -> bool:
        if self.stopped:
            return False
        self._ev.clear()
        self.confirm_request.emit(text)
        self._ev.wait()
        return self._answer and not self.stopped

    def _tick(self, force=False):
        now = time.monotonic()
        if force or now - self._last_emit > 0.06:
            self._last_emit = now
            self.changed.emit()

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
        system = (self.profile.get("system_prompt") or "").replace("{date}", time.strftime("%d.%m.%Y"))
        max_tokens = int(self.profile.get("max_tokens") or 0)
        params = {"temperature": float(self.profile.get("temperature", 0.7))}
        if max_tokens:
            params["max_tokens"] = max_tokens

        for step in range(int(s.get("max_steps", 15))):
            payload, dropped = build_messages(
                system, msgs, self.n_ctx, max_tokens,
                int(s.get("keep_full_tool_results", 2)), int(s.get("old_tool_result_chars", 300)))
            if dropped:
                self.info.emit(f"{dropped} alte Nachrichten passen nicht mehr in den Kontext und wurden ausgelassen.")
            est = estimate_tokens(payload)
            self.stats.emit(f"~{est} Tokens (geschätzt)", est, self.n_ctx or 0)

            self.live = {"content": "", "reasoning": ""}
            calls, usage, timings = {}, None, None
            for chunk in stream_chat(self.ep, self.model, payload, tools, params, lambda: self.stopped):
                usage = chunk.get("usage") or usage
                timings = chunk.get("timings") or timings
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
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
                if timings and timings.get("predicted_per_second"):
                    txt += f" · {timings['predicted_per_second']:.1f} tok/s"
                self.stats.emit(txt, used, self.n_ctx or 0)

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
                        result = self.toolbox.run(name, args, self._confirm)
                    except (json.JSONDecodeError, ValueError):
                        result = "Fehler: Argumente sind kein gültiges JSON-Objekt."
                msgs.append({"role": "tool", "tool_call_id": c["id"], "name": name, "content": result})
                self._tick(force=True)
            self.info.emit("")

        self.info.emit("Maximale Anzahl an Werkzeug-Schritten erreicht.")
