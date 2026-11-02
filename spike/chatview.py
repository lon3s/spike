"""Chatverlauf aus echten Widgets: runde Blasen, Code-Karten mit Kopieren-Knopf, Schatten."""
import html
import re

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


def reasoning_label(text, tail=False):
    t = text.strip()
    t = ("… " + t[-400:]) if tail and len(t) > 400 else (t[:300] + " …" if len(t) > 300 else t)
    return _label(f"💭 <i>{html.escape(t)}</i>", obj="Reasoning")


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

    def _specs(self, messages, live, c, compact):
        """Liste von (key, factory) für alle Blöcke."""
        theme = c.get("name", "")
        results = {m.get("tool_call_id"): m.get("content") or "" for m in messages if m["role"] == "tool"}
        specs, prev = [], None
        if not messages and live is None:
            return [(("welcome", theme), lambda: welcome(c))]
        for i, m in enumerate(messages):
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
                specs.append((("lr", len(rs), theme), lambda: reasoning_label(rs, tail=True)))
            if ct:
                specs.append((("lc", len(ct), theme), lambda: content_widget(ct + " ▍", c)))
            if not rs and not ct:
                specs.append((("dots", theme), lambda: _label(f'<span style="color:{c["muted"]}">● ● ●</span>')))
        return specs

    def render(self, messages, live, c, compact=False):
        specs = self._specs(messages, live, c, compact)
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

    def clear(self):
        for _, w in self.blocks:
            self.col.removeWidget(w)
            w.deleteLater()
        self.blocks = []
