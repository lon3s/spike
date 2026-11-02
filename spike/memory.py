"""Gedächtnis: kurze Fakten über den Nutzer, dauerhaft in ~/.local/share/spike/memory.json.

Damit der Kontext klein bleibt, geht nie das ganze Gedächtnis an das Modell:
- Pro Chat wird einmal (bei der ersten Nachricht) ein Auszug gebildet: angeheftete Fakten, dann die zur
  Nachricht passendsten, dann die neuesten, bis zur Zeichengrenze `memory_prompt_chars`.
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


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\wäöüß.:/-]{3,}", text.lower()) if not w.isdigit() or len(w) > 3}


def add(text: str, pinned=False) -> dict:
    text = " ".join(text.split())[:MAX_FACT]
    with _lock:
        items = load()
        for m in items:                       # Dublette → nur auffrischen
            if m["text"].lower() == text.lower():
                m["updated"] = time.time()
                save(items)
                return m
        m = {"id": uuid.uuid4().hex[:6], "text": text, "pinned": bool(pinned),
             "created": time.time(), "updated": time.time()}
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


def _rank(items, query):
    q = _words(query)
    scored = []
    for m in items:
        w = _words(m["text"])
        hits = len(q & w) + 0.5 * sum(1 for a in q for b in w if a != b and (a in b or b in a) and len(a) > 3)
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

    for m in items:
        if m.get("pinned"):
            take(m)
    for hits, _, m in _rank(items, query):
        if hits > 0:
            take(m)
    for m in sorted(items, key=lambda m: m.get("updated", 0), reverse=True):
        take(m)
    return "\n".join(chosen), len(chosen), len(items)
