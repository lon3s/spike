"""Hintergrundaufträge: lange Befehle (Builds, Downloads, Tests) laufen abgekoppelt weiter.

- job_start startet einen bash-Befehl in eigener Sitzung; die Ausgabe landet in ~/.local/share/spike/jobs/<id>.log.
- Das Modell arbeitet währenddessen weiter oder beendet seine Antwort. Ist der Auftrag fertig, bekommt der Chat,
  aus dem er gestartet wurde, automatisch eine Nachricht mit Rückgabewert und Log-Ende (Controller in tray.py).
- Aufträge überleben einen Neustart von spike: jobs.json merkt sich PID und Log; beim Start wird nachgesehen,
  ob der Prozess noch läuft.
"""
import json
import os
import signal
import subprocess
import threading
import time
import uuid

from .config import DATA_DIR

JOBS_DIR = DATA_DIR / "jobs"
JOBS_FILE = JOBS_DIR / "jobs.json"
_lock = threading.Lock()
JOBS: dict[str, dict] = {}          # id -> {"id","name","command","cwd","pid","log","start","end","rc","chat"}
ON_DONE = []                        # fn(job) – wird im Hintergrund-Thread aufgerufen, wenn ein Auftrag endet
KEEP = 40                           # so viele beendete Aufträge bleiben in der Liste (Logs bleiben länger)


def _save():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(JOBS.values()), ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(JOBS_FILE)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:    # Zombie zählt als beendet
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _finish(job, rc):
    with _lock:
        job["end"] = time.time()
        job["rc"] = rc
        _trim()
        _save()
    for fn in list(ON_DONE):
        try:
            fn(job)
        except Exception as e:
            print("Auftrag-Rückmeldung:", e)


def _trim():
    done = [j for j in JOBS.values() if j.get("end")]
    for j in sorted(done, key=lambda j: j["end"])[:-KEEP] if len(done) > KEEP else []:
        JOBS.pop(j["id"], None)


def _watch_child(job, p: subprocess.Popen):
    rc = p.wait()
    _finish(job, rc)


def _watch_pid(job):
    """Nach einem Neustart von spike: fremden Prozess nur beobachten (kein wait möglich)."""
    pid = job["pid"]
    while _alive(pid):
        time.sleep(2)
    _finish(job, None)


def start(command: str, cwd, chat_id="", name="", env=None, prelude="") -> dict:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jid = uuid.uuid4().hex[:6]
    log = JOBS_DIR / f"{jid}.log"
    name = " ".join((name or command).split())[:60]
    script = f"{prelude}{command}\n"
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(f"# spike-Auftrag {jid} · {time.strftime('%d.%m.%Y %H:%M:%S')} · Ordner {cwd}\n# {command}\n\n")
        fh.flush()
        p = subprocess.Popen(["bash", "-c", script], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=fh,
                             stderr=subprocess.STDOUT, start_new_session=True,
                             env={**os.environ, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat",
                                  "NO_COLOR": "1", **(env or {})})
    job = {"id": jid, "name": name, "command": command, "cwd": str(cwd), "pid": p.pid, "log": str(log),
           "start": time.time(), "end": None, "rc": None, "chat": chat_id or ""}
    with _lock:
        JOBS[jid] = job
        _save()
    threading.Thread(target=_watch_child, args=(job, p), daemon=True).start()
    return job


def get(jid: str) -> dict | None:
    return JOBS.get(jid)


def running() -> list[dict]:
    return [j for j in JOBS.values() if not j.get("end")]


def stop(jid: str) -> str:
    job = JOBS.get(jid)
    if not job:
        return f"Kein Auftrag {jid}."
    if job.get("end"):
        return f"Auftrag {jid} ist schon beendet."
    try:
        os.killpg(job["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return f"Auftrag {jid}: Prozess war schon weg."
    for _ in range(20):
        if not _alive(job["pid"]):
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(job["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    return f"Auftrag {jid} gestoppt."


def tail(jid: str, lines=40, grep="") -> str:
    """Letzte Zeilen des Logs; mit grep nur passende Zeilen (Groß-/Kleinschreibung egal)."""
    import re
    job = JOBS.get(jid)
    if not job:
        return ""
    try:
        text = open(job["log"], encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    rows = text.splitlines()
    if grep:
        rows = [("" if n < 3 and r.startswith("#") else r) for n, r in enumerate(rows)]   # Kopfzeilen nicht treffen
        try:
            rx = re.compile(grep, re.I)
        except re.error:
            rx = re.compile(re.escape(grep), re.I)
        rows = [f"{n + 1}: {r}" for n, r in enumerate(rows) if rx.search(r)]
    rows = [r[:400] for r in rows]
    lines = max(1, int(lines or 40))
    out = rows[-lines:]
    skipped = len(rows) - len(out)
    return (f"[... {skipped} Zeilen davor ...]\n" if skipped > 0 else "") + "\n".join(out)


def _dur(job) -> str:
    s = int((job.get("end") or time.time()) - job["start"])
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def describe(job) -> str:
    if job.get("end"):
        rc = job.get("rc")
        state = ("fertig, exit 0" if rc == 0 else "beendet (Rückgabewert unbekannt)" if rc is None
                 else f"abgebrochen (Signal {-rc})" if rc < 0 else f"FEHLER, exit {rc}")
    else:
        state = "läuft"
    return f"[{job['id']}] {state} · {_dur(job)} · {job['name']}"


def restore():
    """Beim Start: gespeicherte Aufträge einlesen, laufende weiter beobachten."""
    try:
        items = json.loads(JOBS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return
    with _lock:
        for j in items:
            if not j.get("end") and not _alive(int(j.get("pid") or 0)):
                j["end"] = j.get("end") or time.time()
                j["rc"] = None         # spike war aus, als er endete – Rückgabewert unbekannt
            JOBS[j["id"]] = j
        _save()
    for j in running():
        threading.Thread(target=_watch_pid, args=(j,), daemon=True).start()
