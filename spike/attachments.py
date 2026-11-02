"""Anhänge: Dateien, PDFs, Bilder und Screenshots für den Chat.

- Text-/Code-Dateien und PDFs (pdftotext) werden als Text in die Nachricht eingefügt (begrenzt).
- Bilder gehen – wenn der Server Bilder versteht – als image_url (verkleinert, base64) ans Modell.
  Nur die letzten MAX_IMAGES Bilder eines Chats werden mitgeschickt, ältere als kurzer Hinweis (spart Kontext).
- Kopien liegen in ~/.local/share/spike/attachments, damit Chats auch später noch ihre Bilder zeigen.
"""
import base64
import mimetypes
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

ATT_DIR = DATA_DIR / "attachments"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_IMAGES = 3
MAX_SIDE = 1568            # längste Bildseite, die ans Modell geht
IMAGE_TOKENS = 800         # grobe Schätzung pro Bild für die Kontextanzeige


def _store(src: Path, name: str) -> Path:
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    dst = ATT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{name}"
    shutil.copy2(src, dst)
    return dst


def _looks_text(p: Path) -> bool:
    try:
        head = p.read_bytes()[:8192]
    except OSError:
        return False
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            head.decode("latin-1")
            return True
        except Exception:
            return False


def prepare(path) -> dict:
    """Datei prüfen und als Anhang ablegen. Wirft ValueError bei nicht unterstützten Dateien."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"{p} ist keine Datei")
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        kind = "image"
    elif ext == ".pdf":
        kind = "pdf"
    elif _looks_text(p):
        kind = "text"
    else:
        mt = mimetypes.guess_type(p.name)[0] or "unbekannt"
        raise ValueError(f"{p.name}: Dateityp {mt} wird nicht unterstützt (nur Text, Code, PDF, Bilder)")
    stored = _store(p, p.name)
    return {"name": p.name, "path": str(stored), "orig": str(p), "kind": kind, "size": p.stat().st_size}


def from_image(qimage, name="Bild.png") -> dict:
    """Bild aus der Zwischenablage (QImage) als Anhang."""
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    dst = ATT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{name}"
    qimage.save(str(dst), "PNG")
    return {"name": name, "path": str(dst), "orig": "", "kind": "image", "size": dst.stat().st_size}


def screenshot(active_window=False) -> dict:
    """Bildschirmfoto mit Spectacle (KDE). Gibt einen Anhang zurück."""
    ATT_DIR.mkdir(parents=True, exist_ok=True)
    dst = ATT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-screenshot.png"
    if not shutil.which("spectacle"):
        raise ValueError("spectacle ist nicht installiert")
    args = ["spectacle", "-b", "-n", "-a" if active_window else "-f", "-o", str(dst)]
    subprocess.run(args, timeout=30, capture_output=True)
    for _ in range(30):                       # spectacle schreibt die Datei manchmal etwas verzögert
        if dst.exists() and dst.stat().st_size > 0:
            break
        time.sleep(0.1)
    if not dst.exists():
        raise ValueError("Screenshot fehlgeschlagen")
    return {"name": "Screenshot.png", "path": str(dst), "orig": "", "kind": "image", "size": dst.stat().st_size,
            "screenshot": True}


def file_text(att: dict, max_chars: int) -> str:
    """Inhalt einer Text-/PDF-Datei für die Nachricht ans Modell."""
    p = Path(att["path"])
    if att["kind"] == "pdf":
        try:
            r = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=60)
            text = r.stdout
        except Exception as e:
            text = f"(PDF konnte nicht gelesen werden: {e})"
    else:
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    note = ""
    if len(text) > max_chars:
        note = f"\n[… gekürzt: {len(text) - max_chars} von {len(text)} Zeichen fehlen – ganze Datei mit read_file " \
               f"lesen: {att.get('orig') or att['path']}]"
        text = text[:max_chars]
    lang = Path(att["name"]).suffix.lstrip(".") if att["kind"] == "text" else ""
    where = f" ({att['orig']})" if att.get("orig") else ""
    return f"📎 Datei {att['name']}{where}:\n```{lang}\n{text}\n```{note}"


def data_url(path: str) -> str:
    """Bild verkleinern (längste Seite MAX_SIDE) und als data-URL liefern."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage
    img = QImage(path)
    if img.isNull():
        raw = Path(path).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode()
    if max(img.width(), img.height()) > MAX_SIDE:
        img = img.scaled(MAX_SIDE, MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    buf = QByteArray()
    dev = QBuffer(buf)
    dev.open(QIODevice.WriteOnly)
    fmt = "JPEG" if not img.hasAlphaChannel() else "PNG"
    img.save(dev, fmt, 88)
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(bytes(buf.data())).decode()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
