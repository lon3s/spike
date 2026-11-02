"""Chatverlauf aus echten Widgets: runde Blasen, Code-Karten mit Kopieren-Knopf, Schatten."""
import html
import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .theme import md_html
from .tools import short_args

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)(?:```|\Z)", re.S)
MAX_W = 860


def shadow(widget, c, blur=22, dy=4, strength=1.0):
    dark = c.get("name") == "dark"
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, int((150 if dark else 45) * strength)))
    widget.setGraphicsEffect(eff)


def _label(text_html, selectable=True, obj=None):
    lb = QLabel()
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


def user_bubble(text, c, compact, avail=MAX_W):
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
            lb = _label(md_html(seg[1], c["code"]))
            lb.ensurePolished()     # Schriftgröße aus dem Stylesheet übernehmen
            fm = lb.fontMetrics()   # Blase so breit wie der Text (verhindert zu frühes Umbrechen)
            longest = max((fm.horizontalAdvance(l) for l in seg[1].splitlines()), default=0)
            lb.setMinimumWidth(min(int(longest * 1.04) + 10, maxw - 34))
            bl.addWidget(lb)
    bubble.setMaximumWidth(maxw)
    bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
    shadow(bubble, c, blur=20, dy=3)
    lay.addWidget(bubble)
    return row


def tool_chip(call, result, c):
    f = call["function"]
    line = (f'🔧 <b>{html.escape(f["name"])}</b> <span style="color:{c["muted"]}">'
            f'{html.escape(short_args(f["arguments"], 90))}</span>')
    if result is None:
        line += f'<br><span style="color:{c["muted"]}">… läuft</span>'
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


class LiveText(QLabel):
    """Live-Text, der gleichmäßig fließt statt in Schüben zu springen.

    Der Server liefert Tokens in Paketen, die Oberfläche bekommt sie alle ~60 ms. Dieses Label merkt sich,
    wann wie viel Text ankam, und zeigt ihn mit kleiner fester Verzögerung (LAG) so an, dass die Zeichen
    zwischen zwei Paketen gleichmäßig verteilt erscheinen (~60 Bilder/s). Ergebnis: konstantes Tempo,
    kein Stocken und kein Pulsieren. Ist der Rückstand zu groß, wird er sanft aufgeholt.
    """
    FRAME_MS = 16
    LAG = 0.35           # Sekunden hinter dem Server
    MAX_BACKLOG = 300    # Zeichen – mehr Rückstand wird aufgeholt

    def __init__(self, render, obj=None):
        super().__init__()
        self.render = render          # fn(text, done) -> html
        self.target = ""
        self.shown = 0.0
        self._drawn = -1
        self.arrivals = [(time.monotonic(), 0)]
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if obj:
            self.setObjectName(obj)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(self.FRAME_MS)
        self.timer.timeout.connect(self._step)

    def set_target(self, text):
        if text == self.target:
            return
        now = time.monotonic()
        if not text.startswith(self.target[:int(self.shown)]):
            self.shown, self.arrivals = 0.0, [(now, 0)]    # Text ersetzt → von vorn
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
        if total > 6000 and n < total and n - self._drawn < 12:
            return                    # sehr lange Texte seltener neu setzen (Markdown kostet Zeit)
        self._drawn = n
        self.setText(self.render(self.target[:n], n >= total))


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


def role_label(text):
    lb = QLabel(text)
    lb.setObjectName("RoleLabel")
    return lb


def welcome(c):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 110, 0, 0)
    t = QLabel("Wie kann ich helfen?")
    t.setObjectName("WelcomeTitle")
    t.setAlignment(Qt.AlignCenter)
    s = QLabel("Server, Modell und Profil oben wählen · Strg+K = Kompakt-Modus")
    s.setObjectName("Muted")
    s.setAlignment(Qt.AlignCenter)
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


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
        self.col.setSpacing(10)
        self.col.addStretch(1)
        outer.addStretch(1)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)
        self.setWidget(inner)
        self.viewport().setAutoFillBackground(False)
        inner.setAutoFillBackground(False)
        self.blocks = []        # [(key, widget)]
        self.live_widgets = {}  # "rs"/"ct" -> LiveText (bleibt während des Streamens bestehen)
        self._stick = True
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    def _scrolled(self, v):
        sb = self.verticalScrollBar()
        self._stick = v >= sb.maximum() - 40

    def _range_changed(self, _min, mx):
        if self._stick:
            self.verticalScrollBar().setValue(mx)

    def scroll_bottom(self):
        self._stick = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _specs(self, messages, live, c, compact, summary_at=0, summary=""):
        """Liste von (key, factory) für alle Blöcke."""
        theme = c.get("name", "")
        results = {m.get("tool_call_id"): m.get("content") or "" for m in messages if m["role"] == "tool"}
        specs, prev = [], None
        if not messages and live is None:
            return [(("welcome", theme), lambda: welcome(c))]
        for i, m in enumerate(messages):
            if summary_at and i == summary_at:
                specs.append((("sum", i, hash(summary), theme), lambda: summary_divider(summary, c)))
                prev = None
            r = m["role"]
            if r == "user":
                t = m.get("content") or ""
                avail = max(240, self.viewport().width() - 60)
                specs.append((("u", i, hash(t), theme, compact, avail // 40),
                              lambda t=t, a=avail: user_bubble(t, c, compact, a)))
            elif r == "assistant":
                if prev in (None, "user"):
                    specs.append((("lbl", i, theme), lambda: role_label("ASSISTENT")))
                if m.get("_reasoning"):
                    rs = m["_reasoning"]
                    specs.append((("r", i, hash(rs), theme), lambda rs=rs: reasoning_label(rs)))
                if m.get("content"):
                    t = m["content"]
                    specs.append((("a", i, hash(t), theme), lambda t=t: content_widget(t, c)))
                for tc in m.get("tool_calls") or []:
                    res = results.get(tc["id"])
                    specs.append((("t", tc["id"], res is None, theme),
                                   lambda tc=tc, res=res: tool_chip(tc, res, c)))
            else:
                continue
            prev = r
        if live is not None:
            if prev in (None, "user"):
                specs.append((("lbl-live", theme), lambda: role_label("ASSISTENT")))
            rs, ct = live.get("reasoning") or "", live.get("content") or ""
            if rs:
                specs.append((("lr", theme), lambda: self._make_live("rs", c)))
            if ct:
                specs.append((("lc", theme), lambda: self._make_live("ct", c)))
            if not rs and not ct:
                specs.append((("dots", theme), lambda: _label(f'<span style="color:{c["muted"]}">● ● ●</span>')))
        return specs

    def _make_live(self, kind, c):
        if kind == "rs":
            def render(t, done):
                t = t.strip()
                t = ("… " + t[-400:]) if len(t) > 400 else t
                return f"💭 <i>{html.escape(t)}</i>"
            w = LiveText(render, obj="Reasoning")
        else:
            w = LiveText(lambda t, done: md_html(t + ("" if done else " ▍") if t else "▍", c["code"]))
        self.live_widgets[kind] = w
        return w

    def render(self, messages, live, c, compact=False, summary_at=0, summary=""):
        specs = self._specs(messages, live, c, compact, summary_at, summary)
        n = 0
        while n < len(specs) and n < len(self.blocks) and specs[n][0] == self.blocks[n][0]:
            n += 1
        self.setUpdatesEnabled(False)
        for key, w in self.blocks[n:]:
            self.col.removeWidget(w)
            w.deleteLater()
        self.blocks = self.blocks[:n]
        for key, factory in specs[n:]:
            w = factory()
            self.col.insertWidget(self.col.count() - 1, w)   # vor dem Stretch
            self.blocks.append((key, w))
        self.setUpdatesEnabled(True)
        alive = {id(w) for _, w in self.blocks}
        self.live_widgets = {k: w for k, w in self.live_widgets.items() if id(w) in alive}
        if live is not None:
            if "rs" in self.live_widgets:
                self.live_widgets["rs"].set_target(live.get("reasoning") or "")
            if "ct" in self.live_widgets:
                self.live_widgets["ct"].set_target(live.get("content") or "")

    def clear(self):
        for _, w in self.blocks:
            self.col.removeWidget(w)
            w.deleteLater()
        self.blocks = []
