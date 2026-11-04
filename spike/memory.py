"""Gedächtnis: kurze Fakten über den Nutzer, dauerhaft in ~/.local/share/spike/memory.json.

Zwei Arten von Einträgen:
- "user" (📌 Über dich): Name, Wohnort, Webseiten, feste Vorlieben → gehen IMMER in den Prompt.
- "note" (🗂 Arbeitsnotiz): Details zu einer Aufgabe/einem Ordner → nur, wenn die erste Nachricht des Chats
  ausdrücklich dazu passt (ganze Wörter). Sonst findet das Modell sie bei Bedarf mit memory_search.
  Kein Auffüllen mit „neuesten“ Einträgen – sonst landet die letzte Aufgabe in jedem frischen Chat.
Einträge ohne "kind" (ältere Versionen) gelten als "user"; "pinned" heißt ebenfalls „immer“.
- Pro Chat wird der Auszug einmal (bei der ersten Nachricht) gebildet, bis zur Zeichengrenze `memory_prompt_chars`.
- Der Auszug bleibt für den Chat gleich, damit der Prompt-Cache von llama.cpp greift (sonst würde der
  Server den ganzen Verlauf bei jeder Nachricht neu einlesen).
- Den Rest holt sich das Modell bei Bedarf mit memory_search.
"""
import json
import re
import threading
import time
import uuid

from .config import DATA_DIR

MEMORY_FILE = DATA_DIR / "memory.json"
MAX_FACT = 400          # Zeichen pro Eintrag
_lock = threading.Lock()


def load() -> list[dict]:
    try:
        data = json.loads(MEMORY_FILE.read_text("utf-8"))
        return [m for m in data if isinstance(m, dict) and m.get("text")]
    except FileNotFoundError:
        return []
    except Exception as e:
        print("Gedächtnis-Fehler:", e)
        return []


def save(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MEMORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(MEMORY_FILE)


# Füllwörter zählen nicht als Treffer
_STOP = set("""der die das den dem des ein eine einen einem einer eines und oder aber auch noch nur schon mal
mit von für bei aus nach über unter vor zum zur ins im am an auf in zu ist sind war bin bist hat habe hast haben
wir ihr sie er es ich du mir mich dir dich mein meine meinen dein deine sein seine was wie wo wer wann warum
welche welcher welches gibt geht gerade heute jetzt bitte kannst kann könntest soll sollst machen mach mache
dass wenn dann denn doch ja nein nicht kein keine hier dort sehr viel mehr alle alles etwas einfach
the and for you are was with this that what how""".split())


def is_always(m) -> bool:
    return bool(m.get("pinned")) or m.get("kind", "user") != "note"


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\wäöüß]+", text.lower())
            if len(w) >= 3 and w not in _STOP and not w.isdigit()}


def _match(a: str, b: str) -> bool:
    """Ganze Wörter; Wortformen nur bei längeren Wörtern (ordner/ordnern, album/albums) –
    so passt „tag“ (Datum) nicht mehr zu „tags“ (MP3)."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and long_.startswith(short) and len(long_) - len(short) <= 2


def add(text: str, pinned=False, kind="user") -> dict:
    text = " ".join(text.split())[:MAX_FACT]
    with _lock:
        items = load()
        for m in items:                       # Dublette → nur auffrischen
            if m["text"].lower() == text.lower():
                m["updated"] = time.time()
                save(items)
                return m
        m = {"id": uuid.uuid4().hex[:6], "text": text, "pinned": bool(pinned),
             "kind": "note" if kind == "note" else "user", "created": time.time(), "updated": time.time()}
        items.append(m)
        save(items)
        return m


def update(mid: str, text: str) -> bool:
    with _lock:
        items = load()
        for m in items:
            if m["id"] == mid:
                m["text"] = " ".join(text.split())[:MAX_FACT]
                m["updated"] = time.time()
                save(items)
                return True
    return False


def delete(mid: str) -> bool:
    with _lock:
        items = load()
        rest = [m for m in items if m["id"] != mid]
        if len(rest) == len(items):
            return False
        save(rest)
        return True


def set_pinned(mid: str, pinned: bool) -> None:
    with _lock:
        items = load()
        for m in items:
            if m["id"] == mid:
                m["pinned"] = bool(pinned)
        save(items)


def set_kind(mid: str, kind: str) -> None:
    with _lock:
        items = load()
        for m in items:
            if m["id"] == mid:
                m["kind"] = "note" if kind == "note" else "user"
                if kind == "note":
                    m["pinned"] = False
        save(items)


def _rank(items, query):
    q = _words(query)
    scored = []
    for m in items:
        w = _words(m["text"])
        hits = sum(1 for a in q if any(_match(a, b) for b in w))
        scored.append((hits, m.get("updated", 0), m))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored


def search(query: str, limit=12) -> list[dict]:
    items = load()
    if not query.strip():
        return sorted(items, key=lambda m: m.get("updated", 0), reverse=True)[:limit]
    return [m for hits, _, m in _rank(items, query) if hits > 0][:limit]


def snapshot(query: str, max_chars: int) -> tuple[str, int, int]:
    """Auszug für den System-Prompt. Gibt (text, anzahl_im_auszug, anzahl_gesamt) zurück."""
    items = load()
    if not items or max_chars <= 0:
        return "", 0, len(items)
    chosen, seen, used = [], set(), 0

    def take(m):
        nonlocal used
        line = f"- [{m['id']}] {m['text']}"
        if m["id"] in seen or used + len(line) + 1 > max_chars:
            return
        seen.add(m["id"])
        chosen.append(line)
        used += len(line) + 1

    for m in sorted(items, key=lambda m: (not m.get("pinned"), -m.get("updated", 0))):
        if is_always(m):
            take(m)
    for hits, _, m in _rank([m for m in items if not is_always(m)], query):
        if hits > 0:          # Arbeitsnotiz nur, wenn die Nachricht ausdrücklich dazu passt
            take(m)
    return "\n".join(chosen), len(chosen), len(items)
