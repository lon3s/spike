"""Chatverlauf aus echten Widgets: runde Blasen, Code-Karten mit Kopieren-Knopf, Schatten."""
import html
import json
import math
import os
import re
import time
import traceback

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QPropertyAnimation, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QKeySequence, QPainter, QRadialGradient, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QGraphicsOpacityEffect, QGridLayout,
)

from .theme import is_dark, md_html
from .tools import short_args

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
MAX_W = 860


def shadow(widget, c, blur=22, dy=4, strength=1.0):
    dark = is_dark(c)
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, int((150 if dark else 45) * strength)))
    widget.setGraphicsEffect(eff)


def _label(text_html, selectable=True, obj=None):
    lb = _FrozenLabel()
    lb.setTextFormat(Qt.RichText)
    lb.setText(text_html)
    lb.setWordWrap(True)
    lb.setOpenExternalLinks(True)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    if obj:
        lb.setObjectName(obj)
    lb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return lb


def split_code(text):
    """Zerlegt Markdown in [("text", ...), ("code", sprache, code)]."""
    out, pos = [], 0
    for m in _FENCE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        out.append(("code", m.group(1).strip(), m.group(2).rstrip("\n")))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return [s for s in out if s[0] == "code" or s[1].strip()]


class CodeBlock(QFrame):
    def __init__(self, lang, code, c):
        super().__init__()
        self.setObjectName("Code")
        self.code = code
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 10, 12)
        lay.setSpacing(4)
        head = QHBoxLayout()
        tag = QLabel(lang or "code")
        tag.setObjectName("CodeLang")
        self.btn = QPushButton("Kopieren")
        self.btn.setObjectName("CopyBtn")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._copy)
        head.addWidget(tag)
        head.addStretch(1)
        head.addWidget(self.btn)
        lay.addLayout(head)
        body = QLabel(code)
        body.setTextFormat(Qt.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        f.setPointSizeF(10)
        body.setFont(f)
        body.setObjectName("CodeText")
        lay.addWidget(body)
        shadow(self, c, blur=18, dy=3, strength=0.8)

    def _copy(self):
        QApplication.clipboard().setText(self.code)
        self.btn.setText("✓ Kopiert")
        QTimer.singleShot(1500, lambda: self.btn.setText("Kopieren"))


def content_widget(text, c):
    """Assistenten-Text (Markdown) inkl. Code-Karten."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    for seg in split_code(text):
        if seg[0] == "code":
            lay.addWidget(CodeBlock(seg[1], seg[2], c))
        else:
            lay.addWidget(_label(md_html(seg[1], c["code"])))
    return w


_THUMBS = {}


def _thumb(path):
    """Vorschaubild 220×160, zwischengespeichert (volles Einlesen großer Screenshots kostete ~0,3 s pro Bild)."""
    from PySide6.QtGui import QPixmap
    from .attachments import _file_key, load_scaled
    key = _file_key(path)
    if key is None:
        return None
    if key not in _THUMBS:
        img = load_scaled(path, 440, 320)          # doppelt so groß laden, dann sauber verkleinern
        if img.isNull():
            return None
        if len(_THUMBS) > 200:
            _THUMBS.clear()
        _THUMBS[key] = QPixmap.fromImage(img.scaled(220, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return _THUMBS[key]


class ImageViewer(QWidget):
    """Großansicht für Bilder im Chat: passt sich ins Fenster ein, Mausrad = Zoom, Ziehen = Verschieben,
    Doppelklick = Einpassen/100 %, ← → = andere Bilder derselben Nachricht, Esc = schließen."""
    _inst = None

    @classmethod
    def show_images(cls, paths, index=0, parent=None):
        if cls._inst is None:
            cls._inst = cls()
        v = cls._inst
        v.paths, v.i = list(paths), index
        v._load()
        if not v.isVisible():
            scr = (parent.screen() if parent is not None else QApplication.primaryScreen()).availableGeometry()
            img = v.img.size() if not v.img.isNull() else QSize(800, 600)
            w = min(max(img.width() + 40, 520), int(scr.width() * 0.9))
            h = min(max(img.height() + 90, 380), int(scr.height() * 0.9))
            v.resize(w, h)
            v.move(scr.center().x() - w // 2, scr.center().y() - h // 2)
        v.show()
        v.raise_()
        v.activateWindow()

    def __init__(self):
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.paths, self.i = [], 0
        self.img = QImage()
        self.zoom, self.fit = 1.0, True
        self.off = QPointF(0, 0)
        self._drag = None
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 8)
        self.info = QLabel()
        self.info.setStyleSheet("color: #c8c8d0; font-size: 9pt;")
        bar.addWidget(self.info, 1)
        for text, tip, fn in (("◀", "Vorheriges Bild (←)", lambda: self._step(-1)),
                              ("▶", "Nächstes Bild (→)", lambda: self._step(1)),
                              ("⤢", "Einpassen / 100 % (Doppelklick)", self._toggle_fit),
                              ("⧉", "Bild kopieren", self._copy),
                              ("↗", "Im Bildbetrachter öffnen", self._external),
                              ("✕", "Schließen (Esc)", self.close)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedSize(32, 28)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet("QPushButton { color: #e7e7ec; background: rgba(255,255,255,0.08); border: none;"
                            "border-radius: 6px; } QPushButton:hover { background: rgba(255,255,255,0.18); }")
            b.clicked.connect(fn)
            bar.addWidget(b)
            if text in ("◀", "▶"):
                setattr(self, "btn_prev" if text == "◀" else "btn_next", b)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addStretch(1)
        self._bar_h = 44

    def _load(self):
        from PySide6.QtGui import QImageReader
        p = self.paths[self.i] if self.paths else ""
        r = QImageReader(p)
        r.setAutoTransform(True)
        self.img = r.read()
        self.fit, self.off = True, QPointF(0, 0)
        many = len(self.paths) > 1
        self.btn_prev.setVisible(many)
        self.btn_next.setVisible(many)
        self.setWindowTitle(f"spike – {os.path.basename(p)}")
        self._update_info()
        self.update()

    def _area(self):
        return QRectF(0, self._bar_h, self.width(), self.height() - self._bar_h)

    def _fit_zoom(self):
        if self.img.isNull():
            return 1.0
        a = self._area().adjusted(16, 8, -16, -16)
        return min(1.0, a.width() / self.img.width(), a.height() / self.img.height())

    def _scale(self):
        return self._fit_zoom() if self.fit else self.zoom

    def _update_info(self):
        if self.img.isNull():
            self.info.setText("Bild nicht lesbar")
            return
        n = f"{self.i + 1}/{len(self.paths)} · " if len(self.paths) > 1 else ""
        self.info.setText(f"{n}{os.path.basename(self.paths[self.i])} · {self.img.width()}×{self.img.height()} · "
                          f"{self._scale() * 100:.0f} %")

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(14, 14, 18))
        if self.img.isNull():
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        s = self._scale()
        a = self._area()
        w, h = self.img.width() * s, self.img.height() * s
        x = a.x() + (a.width() - w) / 2 + self.off.x()
        y = a.y() + (a.height() - h) / 2 + self.off.y()
        p.setClipRect(a)
        p.drawImage(QRectF(x, y, w, h), self.img)
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_info()

    def wheelEvent(self, e):
        if self.img.isNull():
            return
        old = self._scale()
        new = max(0.05, min(16.0, old * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15)))
        # zum Mauszeiger hin zoomen
        a = self._area()
        c = QPointF(a.center().x(), a.center().y()) + self.off
        m = e.position()
        self.off += (m - c) * (1 - new / old)
        self.zoom, self.fit = new, False
        self._update_info()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = (e.position(), QPointF(self.off))
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag:
            start, off = self._drag
            self.off = off + (e.position() - start)
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.unsetCursor()

    def mouseDoubleClickEvent(self, e):
        self._toggle_fit()

    def _toggle_fit(self):
        if self.fit:
            self.fit, self.zoom = False, 1.0
        else:
            self.fit = True
        self.off = QPointF(0, 0)
        self._update_info()
        self.update()

    def _step(self, d):
        if len(self.paths) > 1:
            self.i = (self.i + d) % len(self.paths)
            self._load()

    def _copy(self):
        if not self.img.isNull():
            QApplication.clipboard().setImage(self.img)
            self.info.setText(self.info.text().split("  ✓")[0] + "  ✓ kopiert")

    def _external(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        if self.paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.paths[self.i]))

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.close()
        elif k in (Qt.Key_Left, Qt.Key_Right):
            self._step(-1 if k == Qt.Key_Left else 1)
        elif k in (Qt.Key_Plus, Qt.Key_Minus, Qt.Key_0):
            if k == Qt.Key_0:
                self.fit = True
            else:
                self.zoom = max(0.05, min(16.0, self._scale() * (1.25 if k == Qt.Key_Plus else 0.8)))
                self.fit = False
            self.off = QPointF(0, 0)
            self._update_info()
            self.update()
        else:
            super().keyPressEvent(e)


def attachment_row(atts, c):
    """Bilder als Vorschau, Dateien als Chips (unter der Nutzer-Nachricht). Klick auf ein Bild = Großansicht."""
    from PySide6.QtGui import QPixmap
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    imgs = [a["path"] for a in atts if a.get("kind") == "image"]
    for a in atts:
        if a.get("kind") == "image":
            pm = _thumb(a["path"])
            lb = QLabel()
            if pm is not None:
                lb.setPixmap(pm)
            else:
                lb.setText("🖼 (Bild fehlt)")
            lb.setToolTip(f"{a.get('name')} – Klick: groß ansehen")
            lb.setCursor(Qt.PointingHandCursor)
            lb.mousePressEvent = lambda e, i=imgs.index(a["path"]), lb=lb: (
                ImageViewer.show_images(imgs, i, lb.window()) if e.button() == Qt.LeftButton else None)
            lb.setStyleSheet(f"border: 1px solid {c['border']}; border-radius: 8px; padding: 2px;")
            lay.addWidget(lb, 0, Qt.AlignTop)
        else:
            icon = "📕" if a.get("kind") == "pdf" else "📄"
            lb = QLabel(f"{icon} {html.escape(a.get('name', ''))}")
            lb.setObjectName("Chip")
            lb.setToolTip(a.get("orig") or a.get("path", ""))
            lb.setStyleSheet("padding: 4px 10px;")
            lay.addWidget(lb, 0, Qt.AlignTop)
    lay.addStretch(0)
    return w


def user_bubble(text, c, compact, avail=MAX_W, atts=None):
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addStretch(1)
    bubble = QFrame()
    bubble.setObjectName("UserBubble")
    bl = QVBoxLayout(bubble)
    bl.setContentsMargins(16, 11, 16, 11)
    bl.setSpacing(8)
    maxw = int(min(MAX_W, avail) * (0.9 if compact else 0.78))
    for seg in split_code(text):
        if seg[0] == "code":
            bl.addWidget(CodeBlock(seg[1], seg[2], c))
        else:
            lb = _label(md_html(seg[1], c.get("user_code", c["code"])))
            lb.ensurePolished()     # Schriftgröße aus dem Stylesheet übernehmen
            fm = lb.fontMetrics()   # Blase so breit wie der Text (verhindert zu frühes Umbrechen)
            longest = max((fm.horizontalAdvance(l) for l in seg[1].splitlines()), default=0)
            lb.setMinimumWidth(min(int(longest * 1.04) + 10, maxw - 34))
            bl.addWidget(lb)
    if atts:
        bl.addWidget(attachment_row(atts, c))
    if not text and not atts:
        bl.addWidget(_label("…"))
    bubble.setMaximumWidth(maxw)
    bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
    lay.addWidget(bubble)
    return row


def _parse_shell_result(res):
    """'exit=0 · Ordner: /x\nausgabe' → (ausgabe, 0, '/x')."""
    if not res:
        return "", None, ""
    first, _, rest = res.partition("\n")
    m = re.match(r"exit=(-?\d+)(?: · Ordner: (.*))?", first)
    if not m:
        return res, None, ""
    return rest, int(m.group(1)), (m.group(2) or "").strip()


def tool_chip(call, result, c, waiting=False):
    f = call["function"]
    line = (f'🔧 <b>{html.escape(f["name"])}</b> <span style="color:{c["muted"]}">'
            f'{html.escape(short_args(f["arguments"], 90))}</span>')
    if result is None:
        line += f'<br><span style="color:{c["muted"]}">{"🔐 wartet auf deine Freigabe" if waiting else "… läuft"}</span>'
    else:
        first = (result.strip().splitlines() or ["(leer)"])[0][:110]
        line += f'<br><span style="color:{c["muted"]}">↳ {html.escape(first)} · {len(result)} Zeichen</span>'
    chip = QFrame()
    chip.setObjectName("Chip")
    lay = QVBoxLayout(chip)
    lay.setContentsMargins(12, 8, 12, 8)
    lb = _label(line, obj="ChipText")
    lay.addWidget(lb)
    shadow(chip, c, blur=14, dy=2, strength=0.6)
    return chip


class TypingDots(QWidget):
    """„KI arbeitet“: drei Punkte, die nacheinander hüpfen und in der Akzentfarbe aufleuchten."""
    PERIOD = 1.4      # Sekunden für eine Welle
    R = 4.0           # Punktradius

    def __init__(self, c):
        super().__init__()
        self.base = QColor(c["muted"])
        self.glow = QColor(c["accent"])
        self.setFixedSize(84, 36)
        self.t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.update)

    def showEvent(self, e):
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    @staticmethod
    def _mix(a, b, k):
        return QColor(int(a.red() + (b.red() - a.red()) * k), int(a.green() + (b.green() - a.green()) * k),
                      int(a.blue() + (b.blue() - a.blue()) * k))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        t = (time.monotonic() - self.t0) / self.PERIOD
        cy = self.height() / 2 + 3
        for i in range(3):
            # durchgehende weiche Welle: jeder Punkt gleitet sanft hoch und wieder runter, leicht versetzt
            k = ((1 - math.cos(2 * math.pi * (t - i * 0.18))) / 2) ** 1.6
            x = 18 + i * 18
            y = cy - 5 * k
            r = self.R * (1 + 0.25 * k)
            if k > 0.02:      # Leuchten: weicher Schein in der Akzentfarbe
                halo = QRadialGradient(x, y, r * 3.2)
                g = QColor(self.glow)
                g.setAlpha(int(140 * k))
                halo.setColorAt(0, g)
                g.setAlpha(0)
                halo.setColorAt(1, g)
                p.setBrush(halo)
                p.drawEllipse(QPointF(x, y), r * 3.2, r * 3.2)
            p.setBrush(self._mix(self.base, self.glow, k))
            p.drawEllipse(QPointF(x, y), r, r)
        p.end()


class _FrozenLabel(QLabel):
    """Label mit unveränderlichem Text: Größen werden gemerkt. QLabel layoutet sonst bei jeder Abfrage den
    ganzen Rich-Text neu – bei vielen eingefrorenen Absätzen kostete das jeden Frame Zeit."""
    def __init__(self):
        super().__init__()
        self._hfw = {}
        self._sh = self._msh = None

    def setText(self, t):
        self._hfw, self._sh, self._msh = {}, None, None
        super().setText(t)

    def heightForWidth(self, w):
        h = self._hfw.get(w)
        if h is None:
            h = self._hfw[w] = super().heightForWidth(w)
        return h

    def sizeHint(self):
        if self._sh is None:
            self._sh = super().sizeHint()
        return self._sh

    def minimumSizeHint(self):
        if self._msh is None:
            self._msh = super().minimumSizeHint()
        return self._msh


class LiveText(QWidget):
    """Live-Text, der gleichmäßig fließt statt in Schüben zu springen.

    Der Server liefert Tokens in Paketen, die Oberfläche bekommt sie alle ~60 ms. Dieses Widget merkt sich,
    wann wie viel Text ankam, und zeigt ihn mit kleiner fester Verzögerung (LAG) so an, dass die Zeichen
    zwischen zwei Paketen gleichmäßig verteilt erscheinen. Ist der Rückstand zu groß, wird er sanft aufgeholt.

    chunked=True: fertige Absätze (bis zur letzten Leerzeile außerhalb von Code-Blöcken) werden einmal gerendert
    und als eigenes Label eingefroren. Pro Frame wird nur noch der offene Rest neu gesetzt – so bleibt die
    Bildzeit konstant, egal wie lang die Antwort wird (Qt layoutet sonst den ganzen Rich-Text bei jedem setText).
    """
    FRAME_MS = 25
    LAG = 0.35           # Sekunden hinter dem Server
    MAX_BACKLOG = 300    # Zeichen – mehr Rückstand wird aufgeholt
    CHUNK_MIN = 600      # Zeichen – kleinere Absätze bleiben im offenen Rest

    def __init__(self, render, obj=None, chunked=False):
        super().__init__()
        self.render = render          # fn(text, done) -> html
        self.chunked = chunked
        self.obj = obj
        self.target = ""
        self.shown = 0.0
        self._drawn = -1
        self._frozen_end = 0          # Zeichen von target, die schon als eingefrorene Labels stehen
        self._frozen = []
        self.arrivals = [(time.monotonic(), 0)]
        # nie breiter als die Spalte: ohne das schiebt eine lange Zeile (Code, Link) den Chat nach rechts raus
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(1)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.tail = self._mk_label()
        self.lay.addWidget(self.tail)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(self.FRAME_MS)
        self.timer.timeout.connect(self._step)

    def _mk_label(self, frozen=False):
        lb = _FrozenLabel() if frozen else QLabel()
        lb.setTextFormat(Qt.RichText)
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lb.setMinimumWidth(1)
        if self.obj:
            lb.setObjectName(self.obj)
        return lb

    def _freeze_upto(self, text):
        """Fertige Absätze von text als eingefrorene Labels vor den offenen Rest setzen."""
        pos = self._frozen_end
        while True:
            i = text.find("\n\n", pos)
            if i < 0:
                return
            nxt = text[i + 2:i + 3]
            if not nxt:
                return                                   # noch nichts nach der Leerzeile
            chunk = text[self._frozen_end:i]
            # nicht mitten im Code-Block, nicht vor Listen/Einrückung/Tabelle/Zitat (Nummerierung bliebe sonst nicht erhalten)
            if (len(chunk) >= self.CHUNK_MIN and chunk.count("```") % 2 == 0
                    and nxt not in " \t-*+0123456789>|"):
                lb = self._mk_label(frozen=True)
                lb.setText(self.render(chunk, True))
                self.lay.insertWidget(len(self._frozen), lb)
                self._frozen.append(lb)
                self._frozen_end = pos = i + 2
            else:
                pos = i + 2

    def _reset_frozen(self):
        for lb in self._frozen:
            self.lay.removeWidget(lb)
            lb.hide()
            lb.deleteLater()
        self._frozen, self._frozen_end, self._drawn = [], 0, -1

    def set_target(self, text):
        if text == self.target:
            return
        now = time.monotonic()
        if not text.startswith(self.target[:int(self.shown)]):
            self.shown, self.arrivals = 0.0, [(now, 0)]    # Text ersetzt → von vorn
            self._reset_frozen()
        self.target = text
        self.arrivals.append((now, len(text)))
        self.arrivals = self.arrivals[-50:]
        if not self.timer.isActive():
            self.timer.start()

    def _curve(self, tau):
        """Wie viele Zeichen zum Zeitpunkt tau angezeigt werden sollen (linear zwischen den Paketen)."""
        arr = self.arrivals
        if tau <= arr[0][0]:
            return arr[0][1]
        for (t0, n0), (t1, n1) in zip(arr, arr[1:]):
            if tau <= t1:
                return n0 + (n1 - n0) * (tau - t0) / max(1e-6, t1 - t0)
        return arr[-1][1]

    def _step(self):
        total = len(self.target)
        if self.shown >= total:
            self.timer.stop()
            return
        want = self._curve(time.monotonic() - self.LAG)
        if total - want > self.MAX_BACKLOG:            # zu weit hinten → zügig, aber weich aufholen
            want = max(want, self.shown + (total - self.MAX_BACKLOG - self.shown) / 8)
        self.shown = min(total, max(self.shown, want))
        n = int(self.shown)
        if n == self._drawn:
            return
        self._drawn = n
        text = self.target[:n]
        if self.chunked:
            if n < self._frozen_end:
                self._reset_frozen()
            self._freeze_upto(text)
            text = text[self._frozen_end:]
        self.tail.setText(self.render(text, n >= total))


TERM = {"bg": "#0e1116", "border": "#262c36", "text": "#d6deeb", "prompt": "#7ee787", "muted": "#7d8590",
        "err": "#ff7b72"}


class TermCard(QFrame):
    """Terminal-Block: „$ befehl“, Ausgabe (live), Exit-Code. Immer dunkel, wie ein echtes Terminal."""
    MAX_LINES = 22

    def __init__(self, command, cwd="", title=""):
        super().__init__()
        self.setObjectName("Term")
        self.setStyleSheet(f"QFrame#Term {{ background: {TERM['bg']}; border: 1px solid {TERM['border']};"
                           f"border-radius: 12px; }}")
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        cwd_txt = f'<span style="color:{TERM["muted"]}">{html.escape(_short_home(cwd))}</span> ' if cwd else ""
        tag = f'<span style="color:{TERM["muted"]}">{html.escape(title)}</span>&nbsp;&nbsp;' if title else ""
        self.cmd = QLabel(f'{tag}{cwd_txt}<span style="color:{TERM["prompt"]}">$</span> '
                          f'<span style="color:{TERM["text"]}">{html.escape(command)}</span>')
        self.cmd.setFont(mono)
        self.cmd.setWordWrap(True)
        self.cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.cmd, 1)
        self.state = QLabel("")
        self.state.setFont(mono)
        head.addWidget(self.state, 0, Qt.AlignTop)
        copy = QPushButton("Kopieren")
        copy.setObjectName("CopyBtn")
        copy.setCursor(Qt.PointingHandCursor)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.out.toPlainText()))
        head.addWidget(copy, 0, Qt.AlignTop)
        lay.addLayout(head)
        self.out = QTextEdit()
        self.out.setReadOnly(True)
        self.out.setAcceptRichText(False)
        self.out.setFont(mono)
        self.out.setFrameShape(QFrame.NoFrame)
        self.out.setLineWrapMode(QTextEdit.WidgetWidth)
        self.out.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.out.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.out.setStyleSheet(f"QTextEdit {{ background: transparent; color: {TERM['text']}; border: none;"
                               f"padding: 0; font-family: '{mono.family()}'; }}")
        self.out.hide()
        lay.addWidget(self.out)
        self._text = None

    def set_output(self, text, running=False, exit_code=None):
        self.state.setText(
            f'<span style="color:{TERM["muted"]}">● läuft</span>' if running else
            "" if exit_code is None else
            f'<span style="color:{TERM["muted"]}">gestoppt</span>' if exit_code < 0 else
            f'<span style="color:{TERM["prompt"] if exit_code == 0 else TERM["err"]}">exit {exit_code}</span>')
        text = (text or "").rstrip("\n")
        if text == self._text:
            return
        self._text = text
        self.out.setVisible(bool(text))
        if not text:
            return
        self.out.setPlainText(text)
        self._fit()

    def _fit(self):
        """Höhe nach Inhalt (Monospace → Zeichen pro Zeile sind exakt), ab MAX_LINES scrollt der Block."""
        if not self._text:
            return
        doc = self.out.document()
        width = self.out.viewport().width()
        if width < 50:
            width = max(200, self.width() - 26)
        doc.setTextWidth(width)
        full = int(doc.size().height()) + 2
        line = self.out.fontMetrics().lineSpacing()
        max_h = self.MAX_LINES * line + int(2 * doc.documentMargin())
        h = min(full, max_h)
        self.out.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if full > max_h else Qt.ScrollBarAlwaysOff)
        if self.out.height() != h:
            self.out.setFixedHeight(h)
        self.out.verticalScrollBar().setValue(self.out.verticalScrollBar().maximum())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit)


def _short_home(path):
    import os
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else path


def diff_view(diff, max_lines=30):
    """Farbiger Diff (rot = weg, grün = neu) im Terminal-Stil."""
    colors = {"+": ("#7ee787", "rgba(46,160,67,0.15)"), "-": ("#ff7b72", "rgba(248,81,73,0.15)"),
              "@": ("#79c0ff", "transparent")}
    rows = []
    lines = diff.splitlines()
    body = [l for l in lines if not l.startswith(("--- ", "+++ "))]
    for l in body[:max_lines]:
        fg, bg = colors.get(l[:1], (TERM["text"], "transparent"))
        rows.append(f'<div style="color:{fg}; background:{bg}; white-space:pre-wrap;">{html.escape(l) or "&nbsp;"}</div>')
    if len(body) > max_lines:
        rows.append(f'<div style="color:{TERM["muted"]}">… {len(body) - max_lines} weitere Zeilen</div>')
    lb = QLabel("".join(rows) or f'<span style="color:{TERM["muted"]}">(keine Änderung)</span>')
    lb.setTextFormat(Qt.RichText)
    lb.setWordWrap(True)
    lb.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lb.setStyleSheet(f"background: {TERM['bg']}; border-radius: 8px; padding: 8px;")
    return lb


def diff_card(path, diff, result, c, undo=None, undone=False, on_undo=None):
    """Erledigte Dateiänderung im Chat: Pfad, +/−-Zähler, farbiger Diff."""
    card = QFrame()
    card.setObjectName("Term")
    card.setStyleSheet(f"QFrame#Term {{ background: {TERM['bg']}; border: 1px solid {TERM['border']};"
                       f"border-radius: 12px; }}")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(12, 8, 12, 10)
    lay.setSpacing(4)
    plus = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    minus = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    head = QLabel(f'<span style="color:{TERM["muted"]}">✎</span> <span style="color:{TERM["text"]}">'
                  f'{html.escape(_short_home(path))}</span>&nbsp;&nbsp;<span style="color:#7ee787">+{plus}</span> '
                  f'<span style="color:#ff7b72">−{minus}</span>')
    head.setTextFormat(Qt.RichText)
    head.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    row = QHBoxLayout()
    row.addWidget(head, 1)
    if undone:
        st = QLabel(f'<span style="color:{TERM["muted"]}">↶ rückgängig gemacht</span>')
        st.setTextFormat(Qt.RichText)
        row.addWidget(st)
    elif undo and on_undo:
        b = QPushButton("↶ Rückgängig")
        b.setObjectName("CopyBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip("Stellt den Stand vor dieser Änderung wieder her (/undo)")
        b.clicked.connect(on_undo)
        row.addWidget(b)
    lay.addLayout(row)
    lay.addWidget(diff_view(diff))
    return card


def confirm_card(pending, c, on_choice):
    """Freigabe direkt im Chat: Erlauben / Immer erlauben / Ablehnen (oder unten schreiben, was stattdessen)."""
    card = QFrame()
    card.setObjectName("Confirm")
    card.setStyleSheet(f"QFrame#Confirm {{ background: {c['panel']}; border: 1px solid {c['accent']};"
                       f"border-radius: 14px; }}")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(8)
    title, _, detail = (pending.get("text") or "").partition("\n\n")
    head = QLabel(f"🔐 <b>{html.escape(title or 'Freigabe nötig')}</b>")
    head.setTextFormat(Qt.RichText)
    lay.addWidget(head)
    if pending.get("diff"):
        if detail:
            path_lb = QLabel(html.escape(detail))
            path_lb.setWordWrap(True)
            path_lb.setObjectName("Muted")
            lay.addWidget(path_lb)
        lay.addWidget(diff_view(pending["diff"], max_lines=40))
    elif detail:
        box = QLabel(html.escape(detail))
        box.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        box.setWordWrap(True)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setStyleSheet(f"background: {TERM['bg']}; color: {TERM['text']}; border-radius: 8px; padding: 8px;")
        lay.addWidget(box)
    row = QHBoxLayout()
    row.setSpacing(8)
    yes = QPushButton("✓ &Erlauben")
    yes.setObjectName("Primary")
    yes.clicked.connect(lambda: on_choice("yes"))
    row.addWidget(yes)
    if pending.get("label"):
        always = QPushButton(f"✓ &Immer: {pending.get('short') or pending['label']}")
        always.setToolTip(f"In diesem Chat nicht mehr fragen bei: {pending['label']}")
        always.clicked.connect(lambda: on_choice("always"))
        row.addWidget(always)
    no = QPushButton("✕ &Ablehnen")
    no.setObjectName("Danger")
    no.clicked.connect(lambda: on_choice("no"))
    row.addWidget(no)
    row.addStretch(1)
    for b in (yes, no):
        b.setCursor(Qt.PointingHandCursor)
    lay.addLayout(row)
    hint = QLabel("…oder unten schreiben, was ich stattdessen tun soll (Enter). Alt+E / Alt+I / Alt+A")
    hint.setObjectName("Muted")
    hint.setStyleSheet("font-size: 8.5pt;")
    hint.setWordWrap(True)
    lay.addWidget(hint)
    shadow(card, c, blur=18, dy=3, strength=0.7)
    QTimer.singleShot(0, yes.setFocus)
    return card


class _AskOption(QFrame):
    """Eine Antwortmöglichkeit: wie ein Knopf bzw. ein Häkchen, aber mit Zeilenumbruch (QPushButton/QCheckBox
    brechen nie um und machen die Karte sonst breiter als das Fenster)."""

    def __init__(self, num, text, c, check=False, primary=False, on_click=None):
        super().__init__()
        self.c, self.check, self.primary, self.on_click = c, check, primary, on_click
        self.checked = False
        self.setObjectName("AskOpt")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 12, 7)
        lay.setSpacing(9)
        self.box = None
        if check:
            self.box = QCheckBox()
            self.box.setFocusPolicy(Qt.NoFocus)
            self.box.setAttribute(Qt.WA_TransparentForMouseEvents)
            lay.addWidget(self.box, 0, Qt.AlignTop)
        num_html = f'<span style="color:{c["muted"] if not primary else c["accent_text"]}">{num}</span>&nbsp; ' if num else ""
        self.lb = QLabel(num_html + html.escape(text))
        self.lb.setTextFormat(Qt.RichText)
        self.lb.setWordWrap(True)
        self.lb.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # Breite kommt von der Karte, nie vom Text
        lay.addWidget(self.lb, 1)
        self._style(False)

    def _style(self, hover):
        c = self.c
        if self.primary:
            bg, fg, bd = (c["accent_hover"] if hover else c["accent"]), c["accent_text"], "transparent"
        else:
            bg, fg = c["panel2"], c["text"]
            bd = c["accent"] if (hover or self.hasFocus() or self.checked) else c["border"]
        self.setStyleSheet(f"QFrame#AskOpt {{ background: {bg}; border: 1px solid {bd}; border-radius: 10px; }}"
                           f"QFrame#AskOpt QLabel {{ color: {fg}; background: transparent; border: none;"
                           f"{' font-weight: 600;' if self.primary else ''} }}")

    def activate(self):
        if self.check:
            self.checked = not self.checked
            self.box.setChecked(self.checked)
            self._style(True)
        if self.on_click:
            self.on_click()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.activate()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.activate()
        else:
            super().keyPressEvent(e)

    def enterEvent(self, e):
        self._style(True)

    def leaveEvent(self, e):
        self._style(False)

    def focusInEvent(self, e):
        self._style(False)
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._style(False)
        super().focusOutEvent(e)


def ask_card(pending, c, on_answer):
    """Rückfrage des Modells (ask_user): eine Möglichkeit anklicken oder mehrere ankreuzen – oder frei antworten."""
    card = QFrame()
    card.setObjectName("Confirm")
    card.setStyleSheet(f"QFrame#Confirm {{ background: {c['panel']}; border: 1px solid {c['accent']};"
                       f"border-radius: 14px; }}")
    card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(8)
    head = QLabel(f"❓ <b>{html.escape(pending.get('question') or 'Frage')}</b>")
    head.setTextFormat(Qt.RichText)
    head.setWordWrap(True)
    head.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    lay.addWidget(head)
    opts, multi = pending.get("options") or [], bool(pending.get("multi"))
    items = []
    if multi:
        ok = QPushButton("✓ Übernehmen")
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setEnabled(False)

        def refresh():
            ok.setEnabled(any(it.checked for it in items))
        for i, o in enumerate(opts):
            it = _AskOption("", o, c, check=True, on_click=refresh)
            items.append(it)
            lay.addWidget(it)
        ok.clicked.connect(lambda: on_answer([o for o, it in zip(opts, items) if it.checked]))
        row = QHBoxLayout()
        row.addWidget(ok)
        row.addStretch(1)
        lay.addLayout(row)
    else:
        for i, o in enumerate(opts):
            it = _AskOption(str(i + 1) if i < 9 else "", o, c, primary=(i == 0),
                            on_click=lambda o=o: on_answer([o]))
            items.append(it)
            lay.addWidget(it)
    for i, it in enumerate(items[:9]):          # Alt+1 … Alt+9
        sc = QShortcut(QKeySequence(f"Alt+{i + 1}"), card)
        sc.activated.connect(it.activate)
    if multi:
        QShortcut(QKeySequence("Alt+Return"), card).activated.connect(lambda: ok.isEnabled() and ok.click())
    hint = QLabel(("Ankreuzen (Alt+Zahl) und übernehmen (Alt+Enter)" if multi
                   else "Eine Möglichkeit anklicken (Alt+Zahl)") + " – oder unten eine eigene Antwort schreiben (Enter).")
    hint.setObjectName("Muted")
    hint.setStyleSheet("font-size: 8.5pt;")
    hint.setWordWrap(True)
    hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    lay.addWidget(hint)
    shadow(card, c, blur=18, dy=3, strength=0.7)
    if items:
        QTimer.singleShot(0, items[0].setFocus)
    return card


def ask_chip(call, result, c):
    """Erledigte Rückfrage im Verlauf: Frage und Antwort des Nutzers."""
    try:
        q = json.loads(call["function"]["arguments"] or "{}").get("question", "")
    except Exception:
        q = ""
    ans = (result or "").replace("Der Nutzer hat gewählt: ", "").replace("Der Nutzer antwortet: ", "")
    line = (f'❓ <b>{html.escape(q or "Rückfrage")}</b><br>'
            f'<span style="color:{c["muted"]}">↳ {html.escape(ans[:300])}</span>')
    chip = QFrame()
    chip.setObjectName("Chip")
    lay = QVBoxLayout(chip)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.addWidget(_label(line, obj="ChipText"))
    shadow(chip, c, blur=14, dy=2, strength=0.6)
    return chip


def reasoning_label(text, tail=False):
    t = text.strip()
    t = ("… " + t[-400:]) if tail and len(t) > 400 else (t[:300] + " …" if len(t) > 300 else t)
    return _label(f"💭 <i>{html.escape(t)}</i>", obj="Reasoning")


def summary_divider(summary, c):
    lb = QLabel("📦  Alles darüber ist zusammengefasst – das Modell sieht nur noch die Zusammenfassung")
    lb.setObjectName("Muted")
    lb.setAlignment(Qt.AlignCenter)
    lb.setWordWrap(True)
    lb.setStyleSheet(f"font-size: 8.5pt; padding: 6px; border-top: 1px dashed {c['border']};"
                     f"border-bottom: 1px dashed {c['border']};")
    lb.setToolTip(summary[:3000])
    return lb


_JOB_HEAD = re.compile(r"(?m)^⚙ Hintergrundauftrag \[")


def job_parts(m) -> tuple[list, str]:
    """Auftrag-Meldungen in einer Nutzer-Nachricht finden (auch zusammengefasste aus der Warteschlange).
    Gibt (Teile für job_card_info, übriger Nutzertext) zurück."""
    if m.get("_job"):
        return [m], ""
    if m.get("_jobparts"):
        return m["_jobparts"], m.get("_text", "")
    content = m.get("content")
    # ältere, zusammengefasste Meldungen ohne Kennzeichnung: am Text erkennen
    if isinstance(content, str) and content.startswith("⚙ Hintergrundauftrag [") and "--- Log-Ende ---" in content:
        starts = [x.start() for x in _JOB_HEAD.finditer(content)] + [len(content)]
        return [{"content": content[a:b].rstrip("\n"), "_job": "?"} for a, b in zip(starts, starts[1:])], ""
    return [], ""


def job_card_info(m) -> dict:
    """Daten für die Karte „Hintergrundauftrag fertig“ – aus _jobinfo oder (ältere Chats) aus dem Text."""
    content = m.get("content") or ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    head, _, tail = content.partition("--- Log-Ende ---\n")
    rows = tail.split("\n")
    ji = dict(m.get("_jobinfo") or {})
    if not ji:
        mm = re.search(r"„(.*?)“ (.*?) nach (.+?)\.\s", head + " ")
        state = mm.group(2) if mm else ""
        sig, ex = re.search(r"Signal (\d+)", state), re.search(r"exit (\d+)", state)
        ji = {"name": mm.group(1) if mm else m.get("_job", ""), "cwd": "", "dur": mm.group(3) if mm else "",
              "rc": -int(sig.group(1)) if sig else int(ex.group(1)) if ex else None}
        for r in rows[:3]:
            mo = re.match(r"# spike-Auftrag \S+ · .*? · Ordner (.*)$", r)
            if mo:
                ji["cwd"] = mo.group(1)
        ji["cmd"] = next((r[2:] for r in rows[:3] if r.startswith("# ") and not r.startswith("# spike-Auftrag ")), "")
    # Kopfzeilen des Logs weglassen – stehen schon im Kartenkopf
    out = [r for r in rows if not r.startswith("# spike-Auftrag ") and r != "# " + ji.get("cmd", "").split("\n")[0]]
    while out and not out[0].strip():
        out.pop(0)
    ji["title"] = f"⚙ {ji.get('name', '')[:40]} · {ji.get('dur', '')}"
    ji["out"] = "\n".join(out)
    return ji


def summary_note(text):
    lb = QLabel(text)
    lb.setObjectName("Muted")
    lb.setAlignment(Qt.AlignCenter)
    lb.setWordWrap(True)
    lb.setStyleSheet("font-size: 8.5pt; padding: 2px;")
    return lb


def role_label(text):
    lb = QLabel(text)
    lb.setObjectName("RoleLabel")
    return lb


class SuggestCard(QFrame):
    """Vorschlagskarte im leeren Chat (klickbar)."""
    def __init__(self, icon, title, text, on_click):
        super().__init__()
        self.setObjectName("SuggestCard")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.on_click = on_click
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        ic = QLabel(icon)
        ic.setObjectName("SuggestIcon")
        lay.addWidget(ic, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setObjectName("SuggestTitle")
        d = QLabel(text)
        d.setObjectName("SuggestText")
        d.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(d)
        lay.addLayout(col, 1)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()) and self.on_click:
            self.on_click()


def _greeting(name):
    h = time.localtime().tm_hour
    g = ("Gute Nacht" if h < 5 else "Guten Morgen" if h < 11 else "Hallo" if h < 17
         else "Guten Abend" if h < 23 else "Gute Nacht")
    return f"{g}, {name}" if name else g


def welcome_big(c, name, resume, on_suggest):
    """Leerer Chat im großen Fenster: Begrüßung + Vorschlagskarten."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 70, 0, 0)
    lay.setSpacing(6)
    t = QLabel(_greeting(name))
    t.setObjectName("WelcomeTitle")
    t.setAlignment(Qt.AlignCenter)
    s = QLabel("Womit fangen wir an?")
    s.setObjectName("WelcomeSub")
    s.setAlignment(Qt.AlignCenter)
    lay.addWidget(t)
    lay.addWidget(s)
    lay.addSpacing(26)
    cards = [
        ("📂", "Datei finden", "Sag mir den Namen oder worum es geht – ich suche überall",
         lambda: on_suggest("fill", "Finde die Datei: ")),
        ("🖥", "System prüfen", "Speicherplatz, RAM, GPU und anstehende Updates",
         lambda: on_suggest("send", "Prüfe meinen PC kurz: freier Speicherplatz, RAM, GPU-Auslastung und ob "
                                    "Updates anstehen. Fasse es knapp zusammen.")),
        ("✍", "Text verbessern", "Rechtschreibung und Stil glätten, Inhalt bleibt",
         lambda: on_suggest("fill", "Verbessere diesen Text (Rechtschreibung und Stil, Inhalt beibehalten):\n\n")),
    ]
    if resume:
        cid, title = resume
        cards.append(("↩", "Weitermachen", f"„{title[:60]}“ fortsetzen", lambda: on_suggest("open", cid)))
    else:
        cards.append(("💡", "Idee ausarbeiten", "Gemeinsam einen Plan oder ein Konzept entwickeln",
                      lambda: on_suggest("fill", "Hilf mir, diese Idee auszuarbeiten: ")))
    grid_w = QWidget()
    grid_w.setMaximumWidth(640)
    grid = QGridLayout(grid_w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(10)
    for i, (ic, title, text, fn) in enumerate(cards):
        grid.addWidget(SuggestCard(ic, title, text, fn), i // 2, i % 2)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(grid_w, 100)
    row.addStretch(1)
    lay.addLayout(row)
    hint = QLabel("Modell und Profil oben in der Mitte · / für Befehle · F1 = Hilfe")
    hint.setObjectName("Muted")
    hint.setAlignment(Qt.AlignCenter)
    lay.addSpacing(14)
    lay.addWidget(hint)
    return w


def welcome(c, compact=False):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 110, 0, 0)
    t = QLabel("Wie kann ich helfen?")
    t.setObjectName("WelcomeTitle")
    t.setAlignment(Qt.AlignCenter)
    s = QLabel("Modell und Profil unten rechts wechseln · F1 = Hilfe" if compact else
               "Server, Modell und Profil oben wählen · F1 = Hilfe")
    s.setObjectName("Muted")
    s.setAlignment(Qt.AlignCenter)
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def _fade_in(w, ms=220):
    """Sanftes Einblenden; der Effekt wird danach wieder entfernt (kostet sonst Leistung)."""
    eff = QGraphicsOpacityEffect(w)
    eff.setOpacity(0.0)
    w.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", w)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: w.setGraphicsEffect(None))
    anim.start()


class ChatView(QScrollArea):
    """Inkrementelle Darstellung: nur geänderte Blöcke werden neu gebaut."""
    MAX_W = MAX_W

    def __init__(self):
        super().__init__()
        self.setObjectName("Chat")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("ChatInner")
        outer = QHBoxLayout(inner)
        outer.setContentsMargins(18, 12, 18, 12)
        self.column = QWidget()
        self.column.setObjectName("ChatColumn")
        self.column.setMaximumWidth(MAX_W)
        self.col = QVBoxLayout(self.column)
        self.col.setContentsMargins(6, 6, 6, 6)
        self.col.setSpacing(14)
        self.col.addStretch(1)
        outer.addStretch(1)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)
        self.setWidget(inner)
        self.viewport().setAutoFillBackground(False)
        inner.setAutoFillBackground(False)
        self.blocks = []        # [(key, widget)]
        self.live_widgets = {}  # "rs"/"ct" -> LiveText (bleibt während des Streamens bestehen)
        self.term_widgets = {}  # ("t", tool_id) / ("u", index) -> TermCard
        self.on_confirm = None  # fn("yes"|"always"|"no")
        self.on_ask = None      # fn([gewählte Möglichkeiten]) – Antwort auf eine ask_user-Rückfrage
        self.on_speak = None    # fn(text) – 🔊 unter der letzten Antwort
        self.on_undo = None     # fn(tool_call_id) – ↶ an einer Diff-Karte
        self.on_suggest = None  # fn(kind, wert) – Vorschlagskarte im leeren Chat ("fill"/"send"/"open")
        self.user_name = ""
        self.resume = None      # (chat_id, titel) – „Weitermachen“-Karte
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        # SPIKE_DEBUG=1 → ~/.cache/spike/scroll.log: wann und warum das Mitscrollen ausgeht
        self._dbg = None
        if os.environ.get("SPIKE_DEBUG"):
            d = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "spike")
            os.makedirs(d, exist_ok=True)
            self._dbg = open(os.path.join(d, "scroll.log"), "a", buffering=1)
            self._last_input = ("-", 0.0)
            self.viewport().installEventFilter(self)
            self.verticalScrollBar().installEventFilter(self)

    def eventFilter(self, obj, ev):
        if getattr(self, "_dbg", None) and ev.type() in (QEvent.Wheel, QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.KeyPress):
            if ev.type() != QEvent.MouseMove or ev.buttons():
                self._last_input = (ev.type().name, time.monotonic())
        return super().eventFilter(obj, ev)

    def _log(self, msg):
        if self._dbg:
            kind, t = self._last_input
            self._dbg.write(f"{time.strftime('%H:%M:%S')} {msg} | letzte Eingabe: {kind} vor "
                            f"{time.monotonic() - t:.1f}s | viewport={self.viewport().height()}\n")

    def _scrolled(self, v):
        sb = self.verticalScrollBar()
        was = self._stick
        self._stick = v >= sb.maximum() - 40
        if self._dbg and was and not self._stick:
            stack = " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(traceback.extract_stack()[:-1][-8:]))
            self._log(f"AUS value={v} max={sb.maximum()} page={sb.pageStep()} stack={stack}")
        elif self._dbg and not was and self._stick:
            self._log(f"AN value={v} max={sb.maximum()}")

    def _range_changed(self, _min, mx):
        if self._stick:
            self.verticalScrollBar().setValue(mx)
        elif self._dbg:
            self._log(f"range max={mx} value={self.verticalScrollBar().value()} (steht, nicht angeheftet)")

    def scroll_bottom(self):
        self._stick = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _specs(self, messages, live, c, compact, summary_at=0, summary="", pending=None, tool_live=None):
        """Liste von (key, factory) für alle Blöcke."""
        theme = c.get("name", "")
        results = {m.get("tool_call_id"): m.get("content") or "" for m in messages if m["role"] == "tool"}
        diffs = {m.get("tool_call_id"): m["_diff"] for m in messages if m["role"] == "tool" and m.get("_diff")}
        tmsgs = {m.get("tool_call_id"): m for m in messages if m["role"] == "tool"}
        specs, prev = [], None
        if not messages and live is None:
            if not compact and self.on_suggest:
                name, resume = self.user_name, self.resume
                return [(("welcome-big", theme, name, resume, _greeting(name)),
                         lambda: welcome_big(c, name, resume, self.on_suggest))]
            return [(("welcome", theme, compact), lambda: welcome(c, compact))]
        for i, m in enumerate(messages):
            if summary_at and i == summary_at:
                specs.append((("sum", i, hash(summary), theme), lambda: summary_divider(summary, c)))
                prev = None
            r = m["role"]
            if r == "user" and m.get("_shell"):
                sh = m["_shell"]
                specs.append((("ush", i, bool(sh.get("running")), theme),
                              lambda sh=sh, i=i: self._make_term(("u", i), sh.get("cmd", ""), sh.get("cwd", ""),
                                                                 "", sh.get("out", ""), sh.get("running"),
                                                                 sh.get("exit"))))
                prev = r
                continue
            parts, rest = job_parts(m) if r == "user" else ([], "")
            if parts:          # Fertig-Meldung(en) von Hintergrundaufträgen → Terminal-Karten
                for k, part in enumerate(parts):
                    ji = job_card_info(part)
                    specs.append((("job", i, k, theme),
                                  lambda ji=ji, i=i, k=k: self._make_term(("j", i, k), ji["cmd"], ji["cwd"],
                                                                          ji["title"], ji["out"], False, ji["rc"])))
                prev = r
                atts = m.get("_attachments") or []
                if not rest and not atts:
                    continue
                m = {**m, "_text": rest}           # was der Nutzer selbst dazu geschrieben hat → normale Blase
            if r == "user" and m.get("_note"):
                specs.append((("note", i, theme), lambda t=m.get("_note"): summary_note(t)))
                continue
            if r == "user":
                atts = m.get("_attachments") or []
                t = m["_text"] if "_text" in m else (m.get("content") or "")
                if m.get("_auto"):
                    t = "📸 Screenshot für das Modell"
                # Breite ohne Scrollleiste messen: sonst ändert sich der Schlüssel, sobald die Leiste
                # erscheint → Blase und laufende Antwort würden neu gebaut und das Scrollen springt
                avail = max(240, self.width() - 60)
                specs.append((("u", i, hash(t), len(atts), theme, compact, avail // 40),
                              lambda t=t, a=avail, atts=atts: user_bubble(t, c, compact, a, atts)))
            elif r == "assistant":
                if prev in (None, "user"):
                    specs.append((("lbl", i, theme), lambda: role_label("✦  spike")))
                if m.get("_reasoning"):
                    rs = m["_reasoning"]
                    specs.append((("r", i, hash(rs), theme), lambda rs=rs: reasoning_label(rs)))
                if m.get("content"):
                    t = m["content"]
                    specs.append((("a", i, hash(t), theme), lambda t=t: content_widget(t, c)))
                for tc in m.get("tool_calls") or []:
                    res = results.get(tc["id"])
                    if tc["function"]["name"] == "ask_user":
                        if res is not None:      # offene Frage zeigt die Karte unten
                            specs.append((("ask", tc["id"], hash(res), theme),
                                          lambda tc=tc, res=res: ask_chip(tc, res, c)))
                        continue
                    if tc["function"]["name"] == "run_shell" and not (res or "").startswith("Vom Nutzer abgelehnt"):
                        try:
                            cmd = json.loads(tc["function"]["arguments"] or "{}").get("command", "")
                        except Exception:
                            cmd = tc["function"]["arguments"]
                        out, code, cwd = _parse_shell_result(res)
                        running = (res is None and not pending and tool_live is not None
                                   and tool_live.get("id") == tc["id"])
                        if res is None and not running and pending:
                            specs.append((("tw", tc["id"], theme), lambda tc=tc: tool_chip(tc, None, c, True)))
                            continue
                        specs.append((("term", tc["id"], res is None, theme),
                                       lambda tc=tc, cmd=cmd, out=out, code=code, cwd=cwd, res=res:
                                       self._make_term(("t", tc["id"]), cmd, cwd, "", out, res is None, code)))
                        continue
                    if tc["id"] in diffs:
                        try:
                            path = json.loads(tc["function"]["arguments"] or "{}").get("path", "")
                        except Exception:
                            path = ""
                        tm = tmsgs.get(tc["id"], {})
                        specs.append((("diff", tc["id"], bool(tm.get("_undone")), theme),
                                      lambda tc=tc, path=path, res=res, tm=tm: diff_card(
                                          path, diffs[tc["id"]], res, c, tm.get("_undo"), bool(tm.get("_undone")),
                                          (lambda: self.on_undo(tc["id"])) if self.on_undo else None)))
                        continue
                    waiting = res is None and bool(pending)
                    specs.append((("t", tc["id"], res is None, waiting, theme),
                                   lambda tc=tc, res=res, waiting=waiting: tool_chip(tc, res, c, waiting)))
            else:
                continue
            prev = r
        last = messages[-1] if messages else None
        if live is None and self.on_speak and last and last["role"] == "assistant" and last.get("content"):
            txt = last["content"]
            specs.append((("speak", len(messages), hash(txt), theme), lambda txt=txt: self._speak_row(txt)))
        if live is not None:
            if prev in (None, "user"):
                specs.append((("lbl-live", theme), lambda: role_label("✦  spike")))
            rs, ct = live.get("reasoning") or "", live.get("content") or ""
            if rs:
                specs.append((("lr", theme), lambda: self._make_live("rs", c)))
            if ct:
                specs.append((("lc", theme), lambda: self._make_live("ct", c)))
            if not rs and not ct and not pending and not tool_live:
                specs.append((("dots", theme), lambda: TypingDots(c)))
        if pending and pending.get("kind") == "ask":
            specs.append((("askcard", id(pending), theme),
                          lambda: ask_card(pending, c, lambda ch: self.on_ask and self.on_ask(ch))))
        elif pending:
            specs.append((("confirm", id(pending), theme),
                          lambda: confirm_card(pending, c, lambda ch: self.on_confirm and self.on_confirm(ch))))
        return specs

    def _speak_row(self, text):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        for label, tip, fn in (("🔊 Vorlesen", "Antwort vorlesen (/vorlesen)", lambda: self.on_speak(text)),
                               ("Kopieren", "Antwort kopieren", lambda: QApplication.clipboard().setText(text))):
            b = QPushButton(label)
            b.setObjectName("CopyBtn")
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            lay.addWidget(b)
        lay.addStretch(1)
        return w

    def _make_term(self, key, cmd, cwd, title, out, running, code):
        w = TermCard(cmd, cwd, title)
        w.set_output(out, running, code)
        self.term_widgets[key] = w
        return w

    def _make_live(self, kind, c):
        if kind == "rs":
            def render(t, done):
                t = t.strip()
                t = ("… " + t[-400:]) if len(t) > 400 else t
                return f"💭 <i>{html.escape(t)}</i>"
            w = LiveText(render, obj="Reasoning")
        else:
            w = LiveText(lambda t, done: md_html(t + ("" if done else " ▍") if t else "▍", c["code"]), chunked=True)
        self.live_widgets[kind] = w
        return w

    def render(self, messages, live, c, compact=False, summary_at=0, summary="", pending=None, tool_live=None):
        specs = self._specs(messages, live, c, compact, summary_at, summary, pending, tool_live)
        n = 0
        while n < len(specs) and n < len(self.blocks) and specs[n][0] == self.blocks[n][0]:
            n += 1
        self.setUpdatesEnabled(False)
        for key, w in self.blocks[n:]:
            self.col.removeWidget(w)
            w.hide()               # sofort weg, nicht erst beim späteren Löschen
            w.deleteLater()
        self.blocks = self.blocks[:n]
        fade = n > 0 and 0 < len(specs) - n <= 3        # nur neu hinzukommende Blöcke, nicht beim Chat-Laden
        for key, factory in specs[n:]:
            w = factory()
            self.col.insertWidget(self.col.count() - 1, w)   # vor dem Stretch
            self.blocks.append((key, w))
            if fade and w.graphicsEffect() is None and not isinstance(w, (LiveText, TypingDots)):
                _fade_in(w)
        self.setUpdatesEnabled(True)
        alive = {id(w) for _, w in self.blocks}
        self.live_widgets = {k: w for k, w in self.live_widgets.items() if id(w) in alive}
        self.term_widgets = {k: w for k, w in self.term_widgets.items() if id(w) in alive}
        if tool_live is not None and ("t", tool_live.get("id")) in self.term_widgets:
            self.term_widgets[("t", tool_live["id"])].set_output(tool_live.get("out", ""), True)
        for i, m in enumerate(messages):      # laufende !-Befehle live nachführen
            sh = m.get("_shell")
            if sh and sh.get("running") and ("u", i) in self.term_widgets:
                self.term_widgets[("u", i)].set_output(sh.get("out", ""), True)
        if live is not None:
            if "rs" in self.live_widgets:
                self.live_widgets["rs"].set_target(live.get("reasoning") or "")
            if "ct" in self.live_widgets:
                self.live_widgets["ct"].set_target(live.get("content") or "")

    def clear(self):
        for _, w in self.blocks:
            self.col.removeWidget(w)
            w.hide()
            w.deleteLater()
        self.blocks = []
