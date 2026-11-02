"""Chatverlauf aus echten Widgets: runde Blasen, Code-Karten mit Kopieren-Knopf, Schatten."""
import html
import json
import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTextEdit,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
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
    if detail:
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
        self.term_widgets = {}  # ("t", tool_id) / ("u", index) -> TermCard
        self.on_confirm = None  # fn("yes"|"always"|"no")
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

    def _specs(self, messages, live, c, compact, summary_at=0, summary="", pending=None, tool_live=None):
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
            if r == "user" and m.get("_shell"):
                sh = m["_shell"]
                specs.append((("ush", i, bool(sh.get("running")), theme),
                              lambda sh=sh, i=i: self._make_term(("u", i), sh.get("cmd", ""), sh.get("cwd", ""),
                                                                 "", sh.get("out", ""), sh.get("running"),
                                                                 sh.get("exit"))))
                prev = r
                continue
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
                    waiting = res is None and bool(pending)
                    specs.append((("t", tc["id"], res is None, waiting, theme),
                                   lambda tc=tc, res=res, waiting=waiting: tool_chip(tc, res, c, waiting)))
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
            if not rs and not ct and not pending and not tool_live:
                specs.append((("dots", theme), lambda: _label(f'<span style="color:{c["muted"]}">● ● ●</span>')))
        if pending:
            specs.append((("confirm", id(pending), theme),
                          lambda: confirm_card(pending, c, lambda ch: self.on_confirm and self.on_confirm(ch))))
        return specs

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
            w = LiveText(lambda t, done: md_html(t + ("" if done else " ▍") if t else "▍", c["code"]))
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
        for key, factory in specs[n:]:
            w = factory()
            self.col.insertWidget(self.col.count() - 1, w)   # vor dem Stretch
            self.blocks.append((key, w))
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
