"""Erinnerungen und geplante Aufgaben.

- Erinnerung: zur Zeit eine KDE-Benachrichtigung.
- Aufgabe: spike schickt den Text zur Zeit an das Modell (Profil „Alles“), ohne Freigaben (alles, was bestätigt
  werden müsste, wird automatisch abgelehnt), speichert das Ergebnis als Chat und meldet es per Benachrichtigung.
  Ein Klick auf „Öffnen“ zeigt den Chat.
- Wiederholung: täglich, werktags, wöchentlich, stündlich oder „alle N min“.
- Läuft nur, solange spike läuft. Verpasste Termine werden beim nächsten Start nachgeholt (als „verpasst“).

Gespeichert in ~/.local/share/spike/tasks.json.
"""
import datetime as dt
import json
import re
import shutil
import subprocess
import threading
import time
import uuid

from .config import DATA_DIR

TASKS_FILE = DATA_DIR / "tasks.json"
_lock = threading.Lock()
WEEKDAYS = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]


def load() -> list[dict]:
    try:
        return json.loads(TASKS_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return []
    except Exception as e:
        print("Aufgaben-Fehler:", e)
        return []


def save(tasks):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(TASKS_FILE)


# ---------- Zeitangaben verstehen ----------

def parse_when(text: str, now: dt.datetime | None = None) -> dt.datetime:
    """'in 20 min', 'in 2 stunden', '15:00', 'morgen 8:30', 'freitag 18 uhr', '2026-11-03 09:00', '3.11. 9:00'."""
    now = now or dt.datetime.now()
    t = text.strip().lower().replace(" uhr", "").replace("uhr", "").strip()
    m = re.fullmatch(r"in\s+(\d+(?:[.,]\d+)?)\s*([a-zäöü]+)", t)
    if m:
        n, unit = float(m.group(1).replace(",", ".")), m.group(2)
        for prefixes, secs in ((("s", "sek", "sec"), 1), (("m", "min"), 60), (("h", "std", "stunde", "hour"), 3600),
                               (("t", "tag", "d", "day"), 86400)):
            if unit in prefixes or unit.startswith(prefixes[1:]):
                return now + dt.timedelta(seconds=n * secs)
        raise ValueError(f"Einheit nicht verstanden: „{unit}“")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ t](\d{1,2})(?::(\d{2}))?", t)
    if m:
        y, mo, d, h, mi = (int(x or 0) for x in m.groups())
        return dt.datetime(y, mo, d, h, mi)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?\s+(\d{1,2})(?::(\d{2}))?", t)
    if m:
        d, mo, y, h, mi = m.groups()
        y = int(y) if y else now.year
        y = y + 2000 if y < 100 else y
        when = dt.datetime(y, int(mo), int(d), int(h), int(mi or 0))
        return when if when > now or m.group(3) else when.replace(year=y + 1)
    day = 0
    for word, add in (("übermorgen", 2), ("morgen", 1), ("heute", 0)):
        if t.startswith(word):
            day, t = add, t[len(word):].strip()
            break
    else:
        for i, name in enumerate(WEEKDAYS):
            if t.startswith(name) or t.startswith(name[:2] + " "):
                day = (i - now.weekday()) % 7 or 7
                t = t.split(" ", 1)[1].strip() if " " in t else "9:00"
                break
    m = re.fullmatch(r"(?:um\s+)?(\d{1,2})(?::(\d{2}))?", t)
    if m:
        when = (now + dt.timedelta(days=day)).replace(hour=int(m.group(1)), minute=int(m.group(2) or 0),
                                                       second=0, microsecond=0)
        if when <= now and day == 0:
            when += dt.timedelta(days=1)          # „15:00“ ist schon vorbei → morgen
        return when
    raise ValueError(f"Zeit nicht verstanden: „{text}“. Beispiele: in 20 min, 15:30, morgen 8:00, "
                     f"freitag 18:00, 2026-11-03 09:00")


def parse_repeat(text: str) -> str:
    t = (text or "").strip().lower()
    if not t or t in ("nein", "keine", "einmal", "none"):
        return ""
    if t.startswith(("tägl", "taegl", "jeden tag", "daily")):
        return "daily"
    if t.startswith(("werktag", "wochentag", "mo-fr", "weekday")):
        return "weekdays"
    if t.startswith(("wöchent", "woechent", "jede woche", "weekly")):
        return "weekly"
    if t.startswith(("stündl", "stuendl", "jede stunde", "hourly")):
        return "hourly"
    m = re.search(r"(\d+)\s*(min|m\b|std|stunde|h\b)", t)
    if m:
        n = int(m.group(1)) * (60 if m.group(2).startswith(("std", "stunde", "h")) else 1)
        return f"every:{max(5, n)}"
    raise ValueError("Wiederholung nicht verstanden (täglich, werktags, wöchentlich, stündlich, alle N min)")


def repeat_label(rep: str) -> str:
    if rep.startswith("every:"):
        n = int(rep.split(":")[1])
        return f"alle {n // 60} h" if n % 60 == 0 else f"alle {n} min"
    return {"daily": "täglich", "weekdays": "werktags", "weekly": "wöchentlich", "hourly": "stündlich"}.get(rep, "")


def next_time(when: dt.datetime, rep: str, now: dt.datetime) -> dt.datetime | None:
    if not rep:
        return None
    step = {"daily": dt.timedelta(days=1), "weekly": dt.timedelta(weeks=1), "hourly": dt.timedelta(hours=1),
            "weekdays": dt.timedelta(days=1)}.get(rep) or dt.timedelta(minutes=int(rep.split(":")[1]))
    nxt = when + step
    while nxt <= now or (rep == "weekdays" and nxt.weekday() >= 5):
        nxt += step
    return nxt


def fmt(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    day = "heute" if d.date() == today else "morgen" if d.date() == today + dt.timedelta(days=1) else \
        ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()] + d.strftime(" %d.%m.")
    return f"{day} {d:%H:%M}"


# ---------- Aufgaben verwalten ----------

def add(when: str, text: str, kind="erinnerung", repeat="", profile="") -> dict:
    t = parse_when(when)
    rep = parse_repeat(repeat)
    kind = "aufgabe" if str(kind).lower().startswith(("auf", "task", "prompt")) else "erinnerung"
    task = {"id": uuid.uuid4().hex[:6], "kind": kind, "text": text.strip(), "next": t.timestamp(), "repeat": rep,
            "profile": profile, "created": time.time()}
    with _lock:
        tasks = load()
        tasks.append(task)
        save(tasks)
    return task


def delete(tid: str) -> bool:
    with _lock:
        tasks = load()
        rest = [t for t in tasks if t["id"] != tid]
        save(rest)
        return len(rest) != len(tasks)


def describe(t: dict) -> str:
    rep = repeat_label(t.get("repeat", ""))
    icon = "⏰" if t["kind"] == "erinnerung" else "🤖"
    return f"{icon} [{t['id']}] {fmt(t['next'])}{' · ' + rep if rep else ''} – {t['text']}"


def due(now_ts: float) -> list[dict]:
    """Fällige Aufgaben holen und neu terminieren (Wiederholung) bzw. entfernen."""
    out = []
    with _lock:
        tasks = load()
        keep = []
        now = dt.datetime.fromtimestamp(now_ts)
        for t in tasks:
            if t["next"] <= now_ts:
                late = now_ts - t["next"] > 300
                out.append({**t, "late": late})
                nxt = next_time(dt.datetime.fromtimestamp(t["next"]), t.get("repeat", ""), now)
                if nxt:
                    keep.append({**t, "next": nxt.timestamp(), "last_run": now_ts})
            else:
                keep.append(t)
        if out:
            save(keep)
    return out


# ---------- Benachrichtigung ----------

LISTENERS = []      # fn(title, body) – z. B. die Handy-Oberfläche


def notify(title: str, body: str, on_open=None, urgent=False):
    """KDE-Benachrichtigung. Mit on_open bekommt sie einen „Öffnen“-Knopf (wartet im Hintergrund auf den Klick)."""
    for fn in LISTENERS:
        try:
            fn(title, body)
        except Exception as e:
            print("Benachrichtigung weitergeben:", e)
    if not shutil.which("notify-send"):
        print("Benachrichtigung:", title, body)
        return
    args = ["notify-send", "-a", "spike", "-i", "spike", "-u", "critical" if urgent else "normal"]
    if on_open:
        args += ["-A", "open=Öffnen", "-w"]

    def job():
        try:
            r = subprocess.run(args + [title, body[:400]], capture_output=True, text=True, timeout=3600)
            if on_open and r.stdout.strip() == "open":
                on_open()
        except Exception as e:
            print("notify-send:", e)
    threading.Thread(target=job, daemon=True).start()
