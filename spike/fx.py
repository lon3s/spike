"""Animierte Hintergrund-Effekte. Die Ebene liegt ganz unten hinter dem Inhalt (Klicks gehen durch):
sie leuchtet nur dort, wo der Hintergrund frei ist – Text, Karten und Code-Blöcke bleiben darüber klar.

CRT = Röhrenmonitor: feine Scanlines, abgedunkelte Ränder (Wölbung), ein heller Bildwiederhol-Balken, der langsam
nach unten rollt, und ab und zu ein kurzes Flackern. Pro Bild wird nur der schmale Streifen um den Balken neu
gemalt – das ganze Fenster nur beim seltenen Flackern –, damit die CPU-Last klein bleibt.
"""
import math
import random
import time

from PySide6.QtCore import QEvent, QLineF, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

FX = [("aus", "Aus"), ("crt", "Röhrenmonitor"), ("knoten", "Knotenfeld (bewegte Knoten)")]


def wanted(settings, colors) -> str:
    mode = settings.get("bg_fx", "aus")
    if mode == "crt_text":          # frühere Auswahl „zitternder Text“ gibt es nicht mehr
        mode = "crt"
    return mode if mode in ("crt", "knoten") else ""     # frühere Auswahl „Vom Design“ (auto) → aus


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


class KnotOverlay(QWidget):
    """Knotenfeld wie auf lon3s.com: wandernde Knoten, Kanten im Umkreis, Signale, die über die Kanten laufen,
    und die Maus zieht Knoten in ihrer Nähe leicht an und lädt sie auf. Liegt hinter dem Chat-Inhalt.
    Bewegung ist zeitbasiert (unabhängig von der Bildrate); läuft nur, solange das Fenster sichtbar ist."""
    FPS = 24
    MAXD = 170          # Reichweite der Kanten
    LINKD = 160         # Reichweite der Maus
    MAXPULSES = 16
    PAL = {
        "dark": {"b": (170, 196, 212), "m": (95, 208, 192), "s": (255, 122, 69), "e": (150, 175, 200),
                 "ea": 0.16, "halo": 0.10},
        "light": {"b": (100, 122, 142), "m": (56, 166, 150), "s": (214, 88, 52), "e": (90, 110, 135),
                  "ea": 0.22, "halo": 0.05},
    }

    def __init__(self, host, radius=0):
        super().__init__(host)
        self.host, self.radius = host, radius
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.pal = self.PAL["dark"]
        self.nodes, self.pulses = [], []
        self._size = None
        self._last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(int(1000 / self.FPS))
        self.timer.timeout.connect(self._frame)
        host.installEventFilter(self)
        self.setGeometry(host.rect())
        self.hide()

    def set_active(self, on, dark=True):
        self.pal = self.PAL["dark" if dark else "light"]
        if on:
            self.setGeometry(self.host.rect())
            self.show()
            self.lower()
            self._last = time.monotonic()
            self.timer.start()
        else:
            self.timer.stop()
            self.hide()

    def eventFilter(self, obj, e):
        if obj is self.host and e.type() == QEvent.Resize:
            self.setGeometry(self.host.rect())
        elif obj is self.host and e.type() == QEvent.Show and self.isVisible():
            self._last = time.monotonic()
            self.timer.start()
        elif obj is self.host and e.type() == QEvent.Hide:
            self.timer.stop()
        return False

    def _seed(self):
        w, h = max(1, self.width()), max(1, self.height())
        n = max(30, min(90, round(w * h / 13500)))
        old = self.nodes
        self.nodes = []
        for i in range(n):
            if i < len(old):        # vorhandene Knoten behalten (kein Neu-Würfeln beim Größe ändern)
                nd = old[i]
                nd["x"], nd["y"] = min(nd["x"], w), min(nd["y"], h)
            else:
                r = random.random()
                nd = {"x": random.random() * w, "y": random.random() * h,
                      "vx": (random.random() - 0.5) * 0.32, "vy": (random.random() - 0.5) * 0.32,
                      "r": 3.2 if random.random() < 0.14 else random.random() * 1.6 + 1,
                      "hue": "s" if r < 0.18 else ("m" if random.random() < 0.35 else "b"), "charge": 0.0}
            self.nodes.append(nd)
        self.pulses = [p for p in self.pulses if p["a"] in self.nodes and p["b"] in self.nodes]
        self._size = (w, h)

    def _frame(self):
        if not self.window().isVisible() or not self.isVisible():
            return
        now = time.monotonic()
        k = min(4.0, (now - self._last) * 60)     # Schritte in „60-fps-Bildern“ wie im Browser
        self._last = now
        if self._size != (self.width(), self.height()):
            self._seed()
        w, h = self.width(), self.height()
        mouse = None
        if self.window().isActiveWindow():
            pt = self.mapFromGlobal(QCursor.pos())
            if self.rect().contains(pt):
                mouse = (pt.x(), pt.y())
        self._mouse = mouse
        for n in self.nodes:
            if mouse:
                dx, dy = mouse[0] - n["x"], mouse[1] - n["y"]
                d = math.hypot(dx, dy)
                if d < self.LINKD:
                    n["charge"] = max(n["charge"], 1 - d / self.LINKD)
                    n["vx"] += dx * 0.0004 * k
                    n["vy"] += dy * 0.0004 * k
            n["x"] += n["vx"] * k
            n["y"] += n["vy"] * k
            n["charge"] *= 0.96 ** k
            if n["x"] < 0 or n["x"] > w:
                n["vx"] *= -1
                n["x"] = max(0, min(w, n["x"]))
            if n["y"] < 0 or n["y"] > h:
                n["vy"] *= -1
                n["y"] = max(0, min(h, n["y"]))
            n["vx"] = max(-0.5, min(0.5, n["vx"]))
            n["vy"] = max(-0.5, min(0.5, n["vy"]))
        for p in self.pulses:
            p["t"] += p["sp"] * k
        self.pulses = [p for p in self.pulses if p["t"] < 1]
        self._k = k
        self.update()

    def paintEvent(self, e):
        if self._size != (self.width(), self.height()):
            self._seed()
        pal = self.pal
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.radius:
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), self.radius, self.radius)
            p.setClipPath(path)
        # Kanten: nach Deckkraft in Stufen gebündelt → wenige Stiftwechsel (schnell genug für Python)
        maxd, maxd2 = self.MAXD, self.MAXD * self.MAXD
        buckets = [[] for _ in range(8)]
        pairs = []
        nodes = self.nodes
        for i, a in enumerate(nodes):
            ax, ay = a["x"], a["y"]
            for b in nodes[i + 1:]:
                dx, dy = ax - b["x"], ay - b["y"]
                d2 = dx * dx + dy * dy
                if d2 < maxd2:
                    kk = 1 - math.sqrt(d2) / maxd
                    pairs.append((a, b))
                    buckets[min(7, int(kk * 8))].append(QLineF(ax, ay, b["x"], b["y"]))
        er, eg, eb = pal["e"]
        for i, lines in enumerate(buckets):
            if lines:
                p.setPen(QPen(QColor(er, eg, eb, int(255 * pal["ea"] * (i + 0.5) / 8)), 0.7))
                p.drawLines(lines)
        mouse = getattr(self, "_mouse", None)
        if mouse:
            mr, mg, mb = pal["m"]
            for n in nodes:
                d = math.hypot(n["x"] - mouse[0], n["y"] - mouse[1])
                if d < self.LINKD:
                    p.setPen(QPen(QColor(mr, mg, mb, int(255 * 0.45 * (1 - d / self.LINKD))), 1))
                    p.drawLine(QPointF(*mouse), QPointF(n["x"], n["y"]))
        if pairs and len(self.pulses) < self.MAXPULSES and random.random() < 0.05 * getattr(self, "_k", 1):
            a, b = random.choice(pairs)
            self.pulses.append({"a": a, "b": b, "t": 0.0, "sp": 0.008 + random.random() * 0.016,
                                "hue": "m" if random.random() < 0.5 else "s"})
        for pu in self.pulses:
            a, b, t = pu["a"], pu["b"], pu["t"]
            x, y = a["x"] + (b["x"] - a["x"]) * t, a["y"] + (b["y"] - a["y"]) * t
            r, g, bb = pal[pu["hue"]]
            p.setPen(QPen(QColor(r, g, bb, 56), 1.2))
            p.drawLine(QPointF(a["x"], a["y"]), QPointF(x, y))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(r, g, bb, 190))
            p.drawEllipse(QPointF(x, y), 1.6, 1.6)
        p.setPen(Qt.NoPen)
        for n in nodes:
            r, g, b = pal[n["hue"]]
            if n["r"] > 2.5:
                p.setBrush(QColor(r, g, b, int(255 * min(1, pal["halo"] + n["charge"] * 0.16))))
                p.drawEllipse(QPointF(n["x"], n["y"]), n["r"] * 3.4, n["r"] * 3.4)
            p.setBrush(QColor(r, g, b, int(255 * min(1, 0.35 + n["charge"] * 0.55))))
            p.drawEllipse(QPointF(n["x"], n["y"]), n["r"], n["r"])
        p.end()
