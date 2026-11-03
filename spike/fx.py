"""Animierte Hintergrund-Effekte, die über dem ganzen Fenster liegen (Klicks gehen durch).

CRT = Röhrenmonitor: feine Scanlines, abgedunkelte Ränder (Wölbung), ein heller Bildwiederhol-Balken, der langsam
nach unten rollt, und ab und zu ein kurzes Flackern. Pro Bild wird nur der schmale Streifen um den Balken neu
gemalt – das ganze Fenster nur beim seltenen Flackern –, damit die CPU-Last klein bleibt.
"""
import random
import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

FX = [("auto", "Vom Design (z. B. Matrix → Röhrenmonitor)"), ("aus", "Aus"), ("crt", "Röhrenmonitor (immer)")]


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
            self.raise_()
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
        dark = QColor(0, 0, 0, 46)
        glow = QColor(self.tint)
        glow.setAlpha(7)
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
            c1.setAlpha(16)
            g.setColorAt(0.0, c0)
            g.setColorAt(0.75, c1)
            g.setColorAt(0.9, QColor(255, 255, 255, 14))
            g.setColorAt(1.0, c0)
            p.fillRect(band, g)
        if self._flicker_until:
            p.setClipPath(self._clip())
            p.fillRect(self.rect(), QColor(255, 255, 255, 9))
        p.end()
