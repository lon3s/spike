"""Eingabeleiste mit wählbarem Stil: Standard, Neon-Glow, Minimal-Linie, Glas.

Die Leiste malt sich je nach Stil selbst. Animationen laufen nur, solange das Eingabefeld den Fokus hat
(oder kurz nach einer Aktion), damit im Leerlauf keine CPU verbraucht wird.
"""
import math
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QConicalGradient, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyleOption, QWidget

STYLES = [
    ("standard", "Standard"),
    ("neon", "Neon-Glow (animiert)"),
    ("linie", "Minimal-Linie (animiert)"),
    ("glas", "Glas (animiert)"),
]


def _col(c, alpha):
    q = QColor(c)
    q.setAlpha(max(0, min(255, int(alpha))))
    return q


def _ease(t):
    return 1 - (1 - max(0.0, min(1.0, t))) ** 3


class Composer(QWidget):
    RADIUS = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Composer")
        self.style_name = "standard"
        self.c = {}
        self.input = None
        self._focus = False
        self._t0 = time.monotonic()
        self._focus_t = 0.0           # Zeitpunkt des letzten Fokus-Wechsels (für Übergänge)
        self._energy = 0.0            # kurzes Aufleuchten beim Tippen/Senden, klingt ab
        self._energy_t = 0.0
        self._sheen_t = -10.0         # Glas: Start des letzten Lichtreflexes
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._frame)

    # --- Verdrahtung ---
    def attach(self, input_box):
        self.input = input_box
        input_box.installEventFilter(self)
        input_box.textChanged.connect(lambda: self.kick(0.6))
        input_box.cursorPositionChanged.connect(self.update)

    def eventFilter(self, obj, e):
        if obj is self.input and e.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self._focus = e.type() == QEvent.FocusIn
            self._focus_t = time.monotonic()
            if self._focus and self.style_name == "glas":
                self._sheen_t = time.monotonic()
            self._wake()
        return False

    def kick(self, amount=1.0):
        """Kurzes Aufleuchten (Tippen 0.6, Senden 1.5)."""
        self._energy = min(1.6, self._cur_energy() + amount)
        self._energy_t = time.monotonic()
        self._wake()

    def _cur_energy(self):
        return self._energy * math.exp(-(time.monotonic() - self._energy_t) * 3.0)

    def _animated(self):
        return self.style_name in ("neon", "linie", "glas")

    def _wake(self):
        if self._animated() and self.isVisible():
            if not self.timer.isActive():
                self.timer.start()
        self.update()

    def _frame(self):
        now = time.monotonic()
        busy = (self._focus or now - self._focus_t < 0.5 or self._cur_energy() > 0.02
                or now - self._sheen_t < 1.4)
        if not busy or not self.isVisible():
            self.timer.stop()
        self.update()

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    # --- Stil setzen ---
    def set_style(self, name, c, compact=False):
        self.style_name = name if name in dict(STYLES) else "standard"
        self.c = c
        lay = self.layout()
        pad = {"neon": 3, "glas": 2}.get(self.style_name, 0)
        if lay is not None:
            lay.setContentsMargins(10 + pad, 6 + pad, 10 + pad, 6 + pad)
        self.timer.stop()
        self._wake()

    def glow_colors(self):
        g = self.c.get("glow") or []
        return [QColor(x) for x in g] or [QColor(self.c.get("accent", "#7c6cf0")),
                                          QColor(self.c.get("accent_hover", "#8f81f5")), QColor("#22d3ee")]

    # --- Malen ---
    def paintEvent(self, e):
        s = self.style_name
        if s == "standard":         # wie bisher: Stylesheet (#Composer) zeichnen lassen
            opt = QStyleOption()
            opt.initFrom(self)
            p = QPainter(self)
            self.style().drawPrimitive(QStyle.PE_Widget, opt, p, self)
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        getattr(self, "_paint_" + s)(p)
        p.end()

    def _shape(self, inset):
        r = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        path = QPainterPath()
        rad = min(self.RADIUS, r.height() / 2)
        path.addRoundedRect(r, rad, rad)
        return r, path, rad

    def _paint_neon(self, p):
        now = time.monotonic() - self._t0
        en = self._cur_energy()
        focus_k = _ease((time.monotonic() - self._focus_t) / 0.4)
        focus_k = focus_k if self._focus else 1 - focus_k
        pulse = 0.5 + 0.5 * math.sin(now * 2.4)
        r, path, rad = self._shape(3.5)
        p.fillPath(path, QColor(self.c.get("panel", "#1b1c22")))
        cols = self.glow_colors()
        grad = QConicalGradient(r.center(), (now * 90) % 360 if (self._focus or en > 0.02) else 90)
        n = len(cols)
        for i, col in enumerate(cols + [cols[0]]):
            grad.setColorAt(i / n, col)
        strength = 0.35 + focus_k * (0.25 + 0.15 * pulse) + 0.35 * en
        p.setBrush(Qt.NoBrush)
        for width, alpha in ((5.0, 0.07), (3.0, 0.12)):    # schmaler, weicher Schein um die Linie
            p.setOpacity(min(1.0, alpha * strength * 2))
            p.setPen(QPen(grad, width))
            p.drawRoundedRect(r, rad, rad)
        p.setOpacity(min(1.0, 0.6 + 0.4 * strength))
        p.setPen(QPen(grad, 1.0))
        p.drawRoundedRect(r, rad, rad)
        p.setOpacity(1.0)

    def _paint_linie(self, p):
        h, w = self.height(), self.width()
        y = h - 2.5
        base = QColor(self.c.get("border", "#30313b"))
        p.setPen(QPen(base, 1.2))
        p.drawLine(QPointF(14, y), QPointF(w - 14, y))
        k = _ease((time.monotonic() - self._focus_t) / 0.35)
        k = k if self._focus else 1 - k
        if k > 0.001:
            acc = QColor(self.c.get("accent", "#7c6cf0"))
            half = (w - 28) / 2 * k
            mid = w / 2
            g = QLinearGradient(QPointF(mid - half, y), QPointF(mid + half, y))
            g.setColorAt(0, _col(acc, 0))
            g.setColorAt(0.15, acc)
            g.setColorAt(0.85, acc)
            g.setColorAt(1, _col(acc, 0))
            p.setPen(QPen(g, 2.2))
            p.drawLine(QPointF(mid - half, y), QPointF(mid + half, y))
            # Lichtpunkt folgt dem Text-Cursor
            if self.input is not None and self._focus:
                cx = self.input.mapTo(self, self.input.cursorRect().center()).x()
                en = self._cur_energy()
                rad = 16 + 18 * en
                glow = QLinearGradient(QPointF(cx - rad, y), QPointF(cx + rad, y))
                hi = QColor(self.c.get("accent_hover", "#ffffff")).lighter(130)
                glow.setColorAt(0, _col(hi, 0))
                glow.setColorAt(0.5, _col(hi, 140 + 110 * min(1.0, en)))
                glow.setColorAt(1, _col(hi, 0))
                p.setPen(QPen(glow, 3.2))
                p.drawLine(QPointF(cx - rad, y), QPointF(cx + rad, y))

    def _paint_glas(self, p):
        r, path, rad = self._shape(3)
        dark = QColor(self.c.get("bg", "#14151a")).lightness() < 128
        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        if dark:
            fill.setColorAt(0, QColor(255, 255, 255, 30))
            fill.setColorAt(1, QColor(255, 255, 255, 12))
        else:
            fill.setColorAt(0, QColor(255, 255, 255, 170))
            fill.setColorAt(1, QColor(255, 255, 255, 110))
        p.fillPath(path, fill)
        # Lichtreflex: ein schräger heller Streifen, der über das Glas zieht
        t = (time.monotonic() - self._sheen_t) / 1.3
        if 0 <= t <= 1:
            p.save()
            p.setClipPath(path)
            x = r.left() - 120 + (r.width() + 240) * _ease(t)
            g = QLinearGradient(QPointF(x - 60, r.top()), QPointF(x + 60, r.bottom()))
            a = 70 if dark else 150
            g.setColorAt(0, QColor(255, 255, 255, 0))
            g.setColorAt(0.5, QColor(255, 255, 255, a))
            g.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillRect(r, g)
            p.restore()
        elif self._focus and time.monotonic() - self._sheen_t > 6.5:
            self._sheen_t = time.monotonic()         # alle paar Sekunden ein neuer Reflex
        # Lichtkante oben, dunklere Kante unten
        edge = QLinearGradient(r.topLeft(), r.bottomLeft())
        edge.setColorAt(0, QColor(255, 255, 255, 120 if dark else 255))
        edge.setColorAt(0.5, QColor(255, 255, 255, 25 if dark else 120))
        edge.setColorAt(1, QColor(0, 0, 0, 40 if dark else 30))
        acc = QColor(self.c.get("accent", "#7c6cf0"))
        k = _ease((time.monotonic() - self._focus_t) / 0.4)
        k = k if self._focus else 1 - k
        p.setPen(QPen(edge, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, rad, rad)
        if k > 0.01:
            p.setPen(QPen(_col(acc, 110 * k), 1.2))
            p.drawRoundedRect(r.adjusted(0.6, 0.6, -0.6, -0.6), rad, rad)
