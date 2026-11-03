"""Animierte Hintergrund-Effekte. Die Ebene liegt ganz unten hinter dem Inhalt (Klicks gehen durch):
sie leuchtet nur dort, wo der Hintergrund frei ist – Text, Karten und Code-Blöcke bleiben darüber klar.

CRT = Röhrenmonitor: feine Scanlines, abgedunkelte Ränder (Wölbung), ein heller Bildwiederhol-Balken, der langsam
nach unten rollt, und ab und zu ein kurzes Flackern. Pro Bild wird nur der schmale Streifen um den Balken neu
gemalt – das ganze Fenster nur beim seltenen Flackern –, damit die CPU-Last klein bleibt.
"""
import random
import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

FX = [("auto", "Vom Design (z. B. Matrix → Röhrenmonitor)"), ("aus", "Aus"), ("crt", "Röhrenmonitor (immer)"),
      ("crt_text", "Röhrenmonitor + zitternder Text")]


def wanted(settings, colors) -> str:
    mode = settings.get("bg_fx", "auto")
    if mode == "auto":
        return colors.get("fx", "")
    return "" if mode == "aus" else mode


class CRTOverlay(QWidget):
    PERIOD = 7.0        # Sekunden, bis der Balken einmal durchgelaufen ist
    BAND = 110          # Höhe des Lichtbalkens in px
    FPS = 30

    def __init__(self, host, radius=0):
        super().__init__(host)
        self.host, self.radius = host, radius
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.tint = QColor("#00ff41")
        self._static = None           # Scanlines + Vignette, einmal pro Größe vorberechnet
        self._t0 = time.monotonic()
        self._last_band = QRect()
        self._flicker_until = 0.0
        self._next_flicker = time.monotonic() + random.uniform(4, 9)
        self.text_flicker = None      # TextFlicker-Ebene: beim Flackern zittert auch der Inhalt
        self.timer = QTimer(self)
        self.timer.setInterval(int(1000 / self.FPS))
        self.timer.timeout.connect(self._frame)
        host.installEventFilter(self)
        self.setGeometry(host.rect())
        self.hide()

    def set_active(self, on, tint=None):
        if tint:
            self.tint = QColor(tint)
            self._static = None
        if on:
            self.setGeometry(self.host.rect())
            self.show()
            self.lower()              # hinter allen anderen Kindern des Fensters
            self.timer.start()
        else:
            self.timer.stop()
            self.hide()

    def eventFilter(self, obj, e):
        if obj is self.host and e.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())
            self._static = None
        elif obj is self.host and e.type() == QEvent.Show and self.isVisible():
            self.timer.start()
        elif obj is self.host and e.type() == QEvent.Hide:
            self.timer.stop()
        return False

    # --- Animation ---
    def _band_rect(self, now):
        h = self.height()
        span = h + self.BAND * 2
        y = int(((now - self._t0) % self.PERIOD) / self.PERIOD * span) - self.BAND
        return QRect(0, y - 2, self.width(), self.BAND + 4)

    def _frame(self):
        if not self.window().isVisible():
            return
        now = time.monotonic()
        if now >= self._next_flicker:                    # kurzes Flackern: ganzes Fenster, 2–3 Bilder lang
            self._flicker_until = now + random.uniform(0.06, 0.12)
            self._next_flicker = now + random.uniform(4, 10)
            self.update()
            if self.text_flicker is not None:
                self.text_flicker.flash()
            return
        if self._flicker_until and now > self._flicker_until:
            self._flicker_until = 0.0
            self.update()
            return
        band = self._band_rect(now)
        self.update(band.united(self._last_band))
        self._last_band = band

    # --- Malen ---
    def _clip(self):
        path = QPainterPath()
        if self.radius:
            path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
        else:
            path.addRect(QRectF(self.rect()))
        return path

    def _make_static(self):
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipPath(self._clip())
        # Scanlines: jede dritte Zeile etwas dunkler, dazwischen ein Hauch Phosphor-Grün
        dark = QColor(0, 0, 0, 70)
        glow = QColor(self.tint)
        glow.setAlpha(12)
        for y in range(0, h, 3):
            p.fillRect(0, y, w, 1, dark)
            p.fillRect(0, y + 1, w, 1, glow)
        # Wölbung: Ränder und Ecken dunkler
        g = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.75)
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(0.6, QColor(0, 0, 0, 0))
        g.setColorAt(1.0, QColor(0, 0, 0, 120))
        p.fillRect(0, 0, w, h, g)
        p.end()
        return pm

    def paintEvent(self, e):
        if self._static is None or self._static.size() != self.size():
            self._static = self._make_static()
        p = QPainter(self)
        p.setClipRect(e.rect())
        p.drawPixmap(0, 0, self._static)
        # rollender Lichtbalken
        band = self._band_rect(time.monotonic())
        if band.intersects(e.rect()):
            p.setClipPath(self._clip())
            p.setClipRect(e.rect(), Qt.IntersectClip)
            g = QLinearGradient(QPointF(0, band.top()), QPointF(0, band.bottom()))
            c0, c1 = QColor(self.tint), QColor(self.tint)
            c0.setAlpha(0)
            c1.setAlpha(34)
            g.setColorAt(0.0, c0)
            g.setColorAt(0.75, c1)
            g.setColorAt(0.9, QColor(255, 255, 255, 22))
            g.setColorAt(1.0, c0)
            p.fillRect(band, g)
        if self._flicker_until:
            p.setClipPath(self._clip())
            p.fillRect(self.rect(), QColor(255, 255, 255, 14))
        p.end()


def _glyph_boxes(arr, bg):
    """arr: HxWx4 (BGRA) eines Textbereichs. Gibt (Alpha-Maske, [(x0, y0, x1, y1), …]) für einzelne Zeichen zurück."""
    import numpy as np
    diff = np.abs(arr[:, :, :3].astype(np.int16) - np.array(bg[:3], np.int16)).sum(axis=2)
    alpha = np.clip((diff - 18) * 4, 0, 255).astype(np.uint8)    # Kantenglättung bleibt weich
    ink = diff > 60
    boxes = []
    rows = np.flatnonzero(ink.any(axis=1))
    if rows.size == 0:
        return alpha, boxes
    # Textzeilen: zusammenhängende Zeilenbereiche mit Tinte (Lücke ≥ 2 px trennt)
    splits = np.flatnonzero(np.diff(rows) > 2)
    for band in np.split(rows, splits + 1):
        y0, y1 = int(band[0]), int(band[-1]) + 1
        cols = np.flatnonzero(ink[y0:y1].any(axis=0))
        if cols.size == 0:
            continue
        csplit = np.flatnonzero(np.diff(cols) > 1)           # leere Spalte trennt Zeichen
        for run in np.split(cols, csplit + 1):
            x0, x1 = int(run[0]), int(run[-1]) + 1
            if x1 - x0 > 40:                                  # zusammenhängende Linie o. Ä. → nicht zittern
                continue
            boxes.append((x0, y0, x1, y1))
    return alpha, boxes


class TextFlicker(QWidget):
    """Beim Flackern zittern die einzelnen Buchstaben (jeder für sich, um 1–2 px), Felder und Rahmen bleiben ruhig.

    Dafür wird einmal ein Abbild aufgenommen, in jedem Textbereich (QLabel, Eingabefeld, Textfelder) die
    Hintergrundfarbe und die einzelnen Zeichen erkannt und diese dann für ein paar Bilder versetzt gezeichnet.
    Braucht numpy; ohne bleibt der Text ruhig. Außerhalb des Flackerns ausgeblendet.
    """
    AMP = [1, 2, 1, 1, 1, 0]           # Stärke pro Bild à 90 ms – meist 1 px, kurz 2 px, klingt aus

    def __init__(self, host, radius=0):
        super().__init__(host)
        self.host, self.radius = host, radius
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.snap = None
        self.parts = []          # [(QRect Bereich, QColor bg, QImage Zeichen mit Alpha, [boxes])]
        self.step = 0
        self.offsets = []
        self.timer = QTimer(self)
        self.timer.setInterval(90)
        self.timer.timeout.connect(self._next)
        self.hide()

    def _text_regions(self):
        from PySide6.QtWidgets import QAbstractScrollArea, QLabel
        out = []
        host_rect = self.host.rect()
        for w in self.host.findChildren(QLabel):
            if w.isVisible() and w.text() and w.pixmap().isNull() and w is not self:
                out.append(w)
        for w in self.host.findChildren(QAbstractScrollArea):
            if w.isVisible() and hasattr(w, "toPlainText") and w.toPlainText():
                out.append(w.viewport())
        rects = []
        for w in out:
            r = QRect(w.mapTo(self.host, w.rect().topLeft()), w.size()).intersected(host_rect)
            if r.width() > 2 and r.height() > 2:
                rects.append(r)
        return rects

    def flash(self):
        if self.isVisible() or not self.host.isVisible():
            return
        try:
            import numpy as np
        except ImportError:
            return
        from PySide6.QtGui import QImage
        self.snap = self.host.grab()
        dpr = self.snap.devicePixelRatio() or 1.0
        img = self.snap.toImage().convertToFormat(QImage.Format_ARGB32)
        W, H = img.width(), img.height()
        full = np.frombuffer(img.constBits(), np.uint8, count=img.sizeInBytes()).reshape(H, img.bytesPerLine())[:, :W * 4]
        full = full.reshape(H, W, 4)
        self.parts = []
        for r in self._text_regions():
            x0, y0 = int(r.x() * dpr), int(r.y() * dpr)
            x1, y1 = min(W, int(r.right() * dpr) + 1), min(H, int(r.bottom() * dpr) + 1)
            arr = full[y0:y1, x0:x1]
            if arr.size == 0:
                continue
            flat = arr[:, :, :3].reshape(-1, 3)
            vals, counts = np.unique(flat[:: max(1, flat.shape[0] // 4000)], axis=0, return_counts=True)
            bg = vals[counts.argmax()]                         # häufigste Farbe = Hintergrund
            alpha, boxes = _glyph_boxes(arr, bg)
            if not boxes:
                continue
            rgba = np.ascontiguousarray(arr.copy())
            rgba[:, :, 3] = alpha
            gi = QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.shape[1] * 4, QImage.Format_ARGB32).copy()
            self.parts.append((QRect(x0, y0, x1 - x0, y1 - y0), QColor(int(bg[2]), int(bg[1]), int(bg[0])), gi, boxes))
        if not self.parts:
            self.snap = None
            return
        self.step = 0
        self._roll()
        self.setGeometry(self.host.rect())
        self.show()
        self.raise_()
        self.timer.start()

    def _roll(self):
        a = self.AMP[min(self.step, len(self.AMP) - 1)]
        self.offsets = [[(random.randint(-a, a), random.randint(-a, a)) for _ in boxes]
                        for _r, _bg, _gi, boxes in self.parts]

    def _next(self):
        self.step += 1
        if self.step >= len(self.AMP):
            self.timer.stop()
            self.hide()
            self.snap, self.parts = None, []
            return
        self._roll()
        self.update()

    def paintEvent(self, e):
        if self.snap is None:
            return
        dpr = self.snap.devicePixelRatio() or 1.0
        p = QPainter(self)
        if self.radius:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
            p.setClipPath(path)
        p.drawPixmap(0, 0, self.snap)
        for (reg, bg, gi, boxes), offs in zip(self.parts, self.offsets):
            ox, oy = reg.x() / dpr, reg.y() / dpr
            for (x0, y0, x1, y1), (dx, dy) in zip(boxes, offs):
                tgt = QRectF(ox + x0 / dpr, oy + y0 / dpr, (x1 - x0) / dpr, (y1 - y0) / dpr)
                p.fillRect(tgt, bg)                          # Zeichen an alter Stelle löschen …
            for (x0, y0, x1, y1), (dx, dy) in zip(boxes, offs):
                src = QRectF(x0, y0, x1 - x0, y1 - y0)
                tgt = QRectF(ox + x0 / dpr + dx, oy + y0 / dpr + dy, (x1 - x0) / dpr, (y1 - y0) / dpr)
                p.drawImage(tgt, gi, src)                    # … und versetzt neu zeichnen
        p.end()
