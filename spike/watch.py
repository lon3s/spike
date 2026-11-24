"""Wachposten: etwas im Auge behalten und melden (oder das Modell reagieren lassen), sobald es passiert.

Ein Wachposten ist ein kleiner Prüf-Befehl (lokal oder auf einem SSH-Rechner), der alle paar Minuten läuft, plus
eine Bedingung. Das Prüfen braucht kein Modell – nur wenn die Bedingung NEU eintritt (vorher nicht erfüllt), wird
gemeldet bzw. das Modell geweckt. Danach meldet er sich erst wieder, wenn die Bedingung zwischendurch weg war.

Bedingungen („wenn“):
  enthaelt     – Ausgabe passt auf ein Muster (regulärer Ausdruck, Groß/klein egal)
  fehlt        – Ausgabe passt NICHT (mehr) auf das Muster
  fehler       – Befehl schlägt fehl (exit ≠ 0), z. B. „ping“/„curl -f“ → Rechner/Seite nicht erreichbar
  ok           – Befehl klappt (exit 0), z. B. „test -e datei“ → Datei ist da
  ueber/unter  – erste Zahl der Ausgabe über/unter dem Wert (z. B. Platte in %)
  aenderung    – Ausgabe anders als beim letzten Mal (Webseite, Version, Dateiliste)

Hauptschalter: settings["watch_enabled"]; jeder Wachposten hat zusätzlich "enabled".
"""
import json
import re
import subprocess
import threading
import time
import uuid

from .config import DATA_DIR

FILE = DATA_DIR / "watches.json"
CONDS = {"enthaelt": "Ausgabe enthält", "fehlt": "Ausgabe enthält nicht (mehr)", "fehler": "Befehl schlägt fehl",
         "ok": "Befehl klappt", "ueber": "Zahl über", "unter": "Zahl unter", "aenderung": "Ausgabe ändert sich"}
MIN_INTERVAL = 1                       # Minuten
_lock = threading.Lock()
WATCHES: dict[str, dict] = {}
ON_CHANGE = []                         # fn() – Liste/Status geändert (für die Anzeige; beliebiger Thread)


def _changed():
    for fn in list(ON_CHANGE):
        try:
            fn()
        except Exception as e:
            print("Wachposten-Anzeige:", e)


def load():
    try:
        items = json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        items = []
    with _lock:
        WATCHES.clear()
        for w in items:
            w["running"] = False
            WATCHES[w["id"]] = w


def save():
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = [{k: v for k, v in w.items() if k != "running"} for w in WATCHES.values()]
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(FILE)


def add(name, command, cond, value="", host="", interval=5, action="melden", task="", chat="", may_act=False) -> dict:
    if cond not in CONDS:
        raise ValueError(f"unbekannte Bedingung „{cond}“ (möglich: {', '.join(CONDS)})")
    if cond in ("enthaelt", "fehlt"):
        re.compile(value or "")
    if cond in ("ueber", "unter"):
        float(str(value).replace(",", "."))
    w = {"id": uuid.uuid4().hex[:5], "name": " ".join(str(name).split())[:60] or command[:40], "command": command,
         "host": host or "", "cond": cond, "value": str(value or ""), "interval": max(MIN_INTERVAL, int(interval or 5)),
         "action": "reagieren" if action == "reagieren" else "melden", "task": task or "", "chat": chat or "",
         "may_act": bool(may_act), "enabled": True, "created": time.time(), "last_check": 0, "last_fire": 0,
         "hits": 0, "state": None, "last_out": "", "last_rc": None, "error": "", "running": False}
    with _lock:
        WATCHES[w["id"]] = w
    save()
    _changed()
    return w


def remove(wid) -> bool:
    with _lock:
        ok = WATCHES.pop(wid, None) is not None
    if ok:
        save()
        _changed()
    return ok


def set_enabled(wid, on: bool):
    w = WATCHES.get(wid)
    if w:
        w["enabled"] = bool(on)
        if on:
            w["state"] = None          # frisch scharf schalten: erster Lauf meldet nur, wenn schon erfüllt
            w["last_check"] = 0
        save()
        _changed()


def describe(w) -> str:
    where = f" auf {w['host']}" if w.get("host") else ""
    val = f" „{w['value']}“" if w.get("value") else ""
    st = "aus" if not w.get("enabled") else ("⚠ ausgelöst" if w.get("state") else "wacht")
    return (f"[{w['id']}] {w['name']} – {st} · alle {w['interval']} min{where} · wenn {CONDS[w['cond']]}{val} "
            f"→ {w['action']} · {w.get('hits', 0)}× ausgelöst")


def _first_number(text):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text or "")
    return float(m.group(0).replace(",", ".")) if m else None


def evaluate(w, rc, out) -> bool | None:
    """Ist die Bedingung erfüllt? None = nicht entscheidbar (z. B. keine Zahl gefunden)."""
    c, v = w["cond"], w.get("value", "")
    if c == "enthaelt":
        return bool(re.search(v, out or "", re.I | re.M))
    if c == "fehlt":
        return not re.search(v, out or "", re.I | re.M)
    if c == "fehler":
        return rc != 0
    if c == "ok":
        return rc == 0
    if c in ("ueber", "unter"):
        n = _first_number(out)
        if n is None:
            return None
        lim = float(str(v).replace(",", "."))
        return n > lim if c == "ueber" else n < lim
    if c == "aenderung":
        prev = w.get("last_out")
        return None if not w.get("last_check") else (out or "").strip() != (prev or "").strip()
    return None


def run_check(w, settings) -> tuple[int, str]:
    """Führt den Prüf-Befehl aus (lokal oder per SSH). Ohne Freigaben, ohne sudo-Passwort, mit Zeitlimit."""
    from . import remote
    timeout = 90
    if w.get("host"):
        h = remote.find(settings, w["host"])
        if not h:
            return 255, f"Rechner „{w['host']}“ ist nicht (mehr) eingerichtet."
        argv = remote.remote_argv(h["target"], w["command"], mark=False)
        stdin = "\n"
    else:
        argv = ["bash", "-c", w["command"]]
        stdin = ""
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, errors="replace", timeout=timeout,
                           env={**__import__("os").environ, "LC_ALL": "C.UTF-8", "TERM": "dumb"})
        return r.returncode, (r.stdout + r.stderr).strip()[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"[Zeitlimit {timeout} s überschritten]"
    except Exception as e:
        return 1, f"[Fehler: {e}]"


def due() -> list[dict]:
    now = time.time()
    with _lock:
        return [w for w in WATCHES.values() if w.get("enabled") and not w.get("running")
                and now - (w.get("last_check") or 0) >= w["interval"] * 60]


def check(w, settings, on_fire):
    """Ein Prüflauf (im Hintergrund-Thread). on_fire(w, rc, out) bei NEU erfüllter Bedingung."""
    w["running"] = True
    try:
        rc, out = run_check(w, settings)
        hit = evaluate(w, rc, out)
        ssh_fail = rc == 255 and w.get("host") and w["cond"] not in ("fehler",)
        w["error"] = out.splitlines()[-1][:200] if ssh_fail and out else ""
        fire = bool(hit) and not w.get("state") and not ssh_fail
        if w["cond"] == "aenderung":         # bei Änderung jedes Mal neu, aber nicht beim ersten Lauf
            fire = bool(hit) and not ssh_fail
        prev_out = w.get("last_out", "")
        w.update(last_check=time.time(), last_rc=rc, last_out=out[-2000:])
        if not ssh_fail and hit is not None:
            w["state"] = bool(hit) if w["cond"] != "aenderung" else False
        if fire:
            w["hits"] = w.get("hits", 0) + 1
            w["last_fire"] = time.time()
        save()
        if fire:
            on_fire(dict(w), rc, out, prev_out)
    finally:
        w["running"] = False
        _changed()
