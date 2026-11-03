"""Farben, Qt-Stylesheet und HTML-Darstellung des Chatverlaufs."""
import html
import re
from functools import lru_cache

from PySide6.QtGui import QColor, QPalette, QTextDocument

from .tools import short_args

THEMES = {
    "dark": {
        "name": "dark", "bg": "#14151a", "panel": "#1b1c22", "panel2": "#24252d", "border": "#30313b",
        "text": "#e7e7ec", "muted": "#8a8c99", "accent": "#7c6cf0", "accent_hover": "#8f81f5",
        "accent_text": "#ffffff", "user": "#2a2748", "user_top": "#7a69f3", "user_bottom": "#5443d2", "user_code": "#4535c2", "code": "#0d0e12", "chip": "#22232b",
        "danger": "#e5676b", "sel": "#2f2b55", "label": "Dunkel", "dark": True, "popup": "#25262c",
    },
    "light": {
        "name": "light", "bg": "#f7f7f9", "panel": "#ffffff", "panel2": "#efeff3", "border": "#dedee5",
        "text": "#1c1c22", "muted": "#6c6e7a", "accent": "#6a5ae0", "accent_hover": "#5a4ad0",
        "accent_text": "#ffffff", "user": "#ebe8ff", "user_top": "#7d6df2", "user_bottom": "#5d4ce0", "user_code": "#4b3bd0", "code": "#f1f1f5", "chip": "#f0f0f4",
        "danger": "#c9444a", "sel": "#e3dfff", "label": "Hell", "dark": False, "popup": "#ececef",
    },
    # Optionale Schlüssel: label (Anzeigename), dark (Schatten/Popup), popup (Popup-Fläche „grau“),
    # user_text (Schrift in der eigenen Sprechblase), font (Schriftfamilie), glow (Farben für den Popup-Rahmen),
    # input (passender Stil der Eingabeleiste, wird beim Wählen im Dialog vorgeschlagen),
    # fx (animierter Hintergrund-Effekt bei „Vom Design“, z. B. crt = Röhrenmonitor)
    "cyberpunk": {
        "name": "cyberpunk", "label": "Cyberpunk", "dark": True,
        "bg": "#0b0218", "panel": "#140630", "panel2": "#1e0b44", "border": "#3d1a6e",
        "text": "#f3e8ff", "muted": "#9d86cf", "accent": "#ff2a6d", "accent_hover": "#ff5289",
        "accent_text": "#ffffff", "user": "#3a0f45", "user_top": "#ff2a6d", "user_bottom": "#7a1fa8",
        "user_code": "#5a0f7a", "code": "#07010f", "chip": "#180838", "danger": "#ff4d4d", "sel": "#3b1160",
        "popup": "#12052a", "glow": ["#ff2a6d", "#05d9e8", "#d1f7ff"], "input": "neon",
    },
    "matrix": {
        "name": "matrix", "label": "Matrix", "dark": True,
        "bg": "#000800", "panel": "#021204", "panel2": "#041c08", "border": "#0c3a14",
        "text": "#a8ffb6", "muted": "#3a9a4c", "accent": "#00e639", "accent_hover": "#3dff6a",
        "accent_text": "#001a06", "user": "#03260c", "user_top": "#0a5a1e", "user_bottom": "#043a12",
        "user_code": "#022a0c", "code": "#000400", "chip": "#03170a", "danger": "#ff5050", "sel": "#073814",
        "popup": "#010e03", "user_text": "#d4ffdc",
        "font": "'JetBrains Mono', 'Fira Code', 'Hack', 'DejaVu Sans Mono', monospace",
        "glow": ["#00ff41", "#008f11", "#b8ffc4"], "input": "neon", "fx": "crt",
    },
    "solarized": {
        "name": "solarized", "label": "Solarized dunkel", "dark": True,
        "bg": "#002b36", "panel": "#073642", "panel2": "#0b404d", "border": "#1b505d",
        "text": "#c4cfcf", "muted": "#7b9399", "accent": "#268bd2", "accent_hover": "#3d9ae0",
        "accent_text": "#ffffff", "user": "#0d4459", "user_top": "#268bd2", "user_bottom": "#1b6aa5",
        "user_code": "#155a8c", "code": "#00212a", "chip": "#06323d", "danger": "#dc322f", "sel": "#0e4a5a",
        "popup": "#04313d", "glow": ["#268bd2", "#2aa198", "#b58900"],
    },
    "solarized_light": {
        "name": "solarized_light", "label": "Solarized hell", "dark": False,
        "bg": "#fdf6e3", "panel": "#fffbef", "panel2": "#eee8d5", "border": "#ddd5bd",
        "text": "#3f5359", "muted": "#7f8f91", "accent": "#268bd2", "accent_hover": "#1d76b6",
        "accent_text": "#ffffff", "user": "#e2edf2", "user_top": "#268bd2", "user_bottom": "#1b6aa5",
        "user_code": "#155a8c", "code": "#f2ebd6", "chip": "#f4eedb", "danger": "#dc322f", "sel": "#dde8e6",
        "popup": "#f3ecd8", "glow": ["#268bd2", "#2aa198", "#cb4b16"],
    },
    "nord": {
        "name": "nord", "label": "Nord", "dark": True,
        "bg": "#2e3440", "panel": "#353c4a", "panel2": "#3d4556", "border": "#4c566a",
        "text": "#eceff4", "muted": "#9aa5b8", "accent": "#88c0d0", "accent_hover": "#9ccddb",
        "accent_text": "#242933", "user": "#3b4a5e", "user_top": "#5e81ac", "user_bottom": "#4a6890",
        "user_code": "#3d5779", "code": "#262b35", "chip": "#363d4b", "danger": "#bf616a", "sel": "#44526a",
        "popup": "#333a47", "glow": ["#88c0d0", "#81a1c1", "#b48ead"],
    },
    "dracula": {
        "name": "dracula", "label": "Dracula", "dark": True,
        "bg": "#21222c", "panel": "#282a36", "panel2": "#323445", "border": "#44475a",
        "text": "#f8f8f2", "muted": "#8a93c4", "accent": "#bd93f9", "accent_hover": "#cba8fb",
        "accent_text": "#1e1f29", "user": "#3a3360", "user_top": "#9b6cf2", "user_bottom": "#6d43c8",
        "user_code": "#5935a8", "code": "#191a21", "chip": "#2a2c3a", "danger": "#ff5555", "sel": "#44475a",
        "popup": "#272935", "glow": ["#bd93f9", "#ff79c6", "#8be9fd"],
    },
    "gruvbox": {
        "name": "gruvbox", "label": "Gruvbox", "dark": True,
        "bg": "#1d2021", "panel": "#282828", "panel2": "#32302f", "border": "#504945",
        "text": "#ebdbb2", "muted": "#a89984", "accent": "#fe8019", "accent_hover": "#fe9644",
        "accent_text": "#1d2021", "user": "#3c3026", "user_top": "#d65d0e", "user_bottom": "#a53e05",
        "user_code": "#8a3304", "code": "#161819", "chip": "#2a2827", "danger": "#fb4934", "sel": "#45403d",
        "popup": "#262625", "glow": ["#fe8019", "#fabd2f", "#b8bb26"],
    },
}


def is_dark(c) -> bool:
    return bool(c.get("dark", c.get("name") != "light"))


def palette(c) -> QPalette:
    p = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "panel"), (QPalette.AlternateBase, "panel2"),
                      (QPalette.Button, "panel2"), (QPalette.Text, "text"), (QPalette.WindowText, "text"),
                      (QPalette.ButtonText, "text"), (QPalette.ToolTipBase, "panel2"), (QPalette.ToolTipText, "text"),
                      (QPalette.Highlight, "accent"), (QPalette.HighlightedText, "accent_text"),
                      (QPalette.PlaceholderText, "muted"), (QPalette.Link, "accent")):
        p.setColor(role, QColor(c[key]))
    return p


def qss(c) -> str:
    return f"""
    QWidget {{ color: {c['text']}; font-size: 10.5pt;{f" font-family: {c['font']};" if c.get('font') else ""} }}
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QMainWindow#Popup {{ background: transparent; }}
    #Side {{ background: {c['panel']}; border-right: 1px solid {c['border']}; }}
    #TopBar {{ background: {c['bg']}; border-bottom: 1px solid {c['border']}; }}
    #Composer {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 20px; }}
    QScrollArea#Chat, #ChatInner, #ChatColumn {{ background: {c['bg']}; border: none; }}
    #UserBubble {{ background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1, stop:0 {c['user_top']},
                   stop:1 {c['user_bottom']}); border: 1px solid rgba(255, 255, 255, 30);
                   border-radius: 18px; border-bottom-right-radius: 8px; }}
    #UserBubble QLabel {{ color: {c.get('user_text', '#ffffff')}; background: transparent; }}
    #Code {{ background: {c['code']}; border: 1px solid {c['border']}; border-radius: 12px; }}
    #CodeLang {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 600; }}
    #CodeText {{ color: {c['text']}; background: transparent; }}
    QPushButton#CopyBtn {{ background: transparent; border: 1px solid {c['border']}; border-radius: 7px;
                          padding: 2px 9px; font-size: 8.5pt; color: {c['muted']}; }}
    QPushButton#CopyBtn:hover {{ color: {c['text']}; border-color: {c['accent']}; }}
    #Chip {{ background: {c['chip']}; border: 1px solid {c['border']}; border-radius: 12px; }}
    #ChipText {{ font-size: 9pt; }}
    #Reasoning {{ color: {c['muted']}; font-size: 9pt; }}
    #RoleLabel {{ color: {c['muted']}; font-size: 8.5pt; font-weight: 700; padding-top: 6px; }}
    #WelcomeTitle {{ font-size: 22pt; font-weight: 700; }}
    #PopupFrame {{ background: {c['bg']}; border: 1px solid {c['border']}; border-radius: 18px; }}
    #PopupFrame #Root, #PopupFrame #TopBar, #PopupFrame QScrollArea#Chat, #PopupFrame #ChatInner,
    #PopupFrame #ChatColumn, #PopupFrame QScrollArea#Chat > QWidget {{ background: transparent; }}
    QPushButton#Icon:checked {{ background: {c['sel']}; color: {c['accent']}; }}
    #Composer:focus-within {{ border-color: {c['accent']}; }}
    QTextBrowser#Chat {{ background: {c['bg']}; border: none; }}
    QPlainTextEdit#Input {{ background: transparent; border: none; padding: 4px; }}
    QListWidget#Chats {{ background: transparent; border: none; outline: none; }}
    QListWidget#Chats::item {{ padding: 8px 38px 8px 10px; border-radius: 8px; margin: 1px 0; color: {c['muted']}; }}
    QPushButton#ArchiveBtn {{ background: transparent; border: 1px solid transparent; color: {c['muted']};
                             text-align: left; padding: 6px 10px; }}
    QPushButton#ArchiveBtn:hover {{ background: {c['panel2']}; color: {c['text']}; }}
    QPushButton#ArchiveBtn:checked {{ background: {c['sel']}; color: {c['text']}; }}
    QListWidget#Chats::item:hover {{ background: {c['panel2']}; color: {c['text']}; }}
    QListWidget#Chats::item:selected {{ background: {c['sel']}; color: {c['text']}; }}
    QPushButton {{ background: {c['panel2']}; border: 1px solid {c['border']}; border-radius: 10px;
                  padding: 6px 12px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:disabled {{ color: {c['muted']}; background: {c['panel']}; }}
    QPushButton#Primary {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 600; }}
    QPushButton#Primary:hover {{ background: {c['accent_hover']}; }}
    QPushButton#Primary:disabled {{ background: {c['panel2']}; color: {c['muted']}; }}
    QPushButton#Icon {{ background: transparent; border: none; padding: 4px 8px; border-radius: 8px;
                       color: {c['muted']}; font-size: 13pt; }}
    QPushButton#Icon:hover {{ background: {c['panel2']}; color: {c['text']}; }}
    QPushButton#Danger:hover {{ border-color: {c['danger']}; color: {c['danger']}; }}
    QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {c['panel']}; border: 1px solid {c['border']};
                  border-radius: 10px; padding: 5px 9px; }}
    QComboBox:hover, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['border']};
                  selection-background-color: {c['sel']}; selection-color: {c['text']}; outline: none; }}
    QPlainTextEdit {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
    QListWidget {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
    QListWidget::item {{ padding: 6px; }}
    QListWidget::item:selected {{ background: {c['sel']}; color: {c['text']}; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; top: -1px; }}
    QTabBar::tab {{ background: transparent; padding: 8px 14px; color: {c['muted']}; border: none; }}
    QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
    QStatusBar {{ background: {c['bg']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
    QStatusBar QLabel {{ color: {c['muted']}; font-size: 9.5pt; }}
    QProgressBar {{ background: {c['panel2']}; border: none; border-radius: 4px; height: 8px;
                   color: {c['muted']}; text-align: center; font-size: 8.5pt; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
                   background: none; height: 0; }}
    QSplitter::handle {{ background: {c['border']}; width: 1px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; }}
    QToolTip {{ background: {c['panel2']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
    QLabel#Muted {{ color: {c['muted']}; }}
    QLabel#MetaText {{ color: {c['muted']}; font-size: 8.5pt; }}
    QToolButton#MetaBtn {{ color: {c['muted']}; font-size: 8.5pt; background: transparent; border: none;
                          border-radius: 6px; padding: 1px 6px; }}
    QToolButton#MetaBtn:hover {{ color: {c['text']}; background: {c['panel2']}; }}
    QToolButton#MetaBtn::menu-indicator {{ image: none; width: 0; }}
    QMenu {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 8px;
            padding: 4px; }}
    QMenu::item {{ padding: 4px 20px 4px 24px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {c['sel']}; }}
    QMenu::item:disabled {{ color: {c['muted']}; }}
    QMenu::section {{ color: {c['muted']}; font-size: 8pt; padding: 6px 8px 2px 8px; background: transparent; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 6px; }}
    QListWidget#CmdList {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 12px;
                          padding: 4px; outline: none; }}
    QListWidget#CmdList::item {{ padding: 4px 8px; border-radius: 7px; color: {c['text']}; }}
    QListWidget#CmdList::item:selected {{ background: {c['sel']}; color: {c['text']}; }}
    """


# ---------- Markdown → HTML ----------

_BODY = re.compile(r"<body[^>]*>(.*)</body>", re.S)
_PRE_RUN = re.compile(r"((?:<pre[^>]*>.*?</pre>\s*)+)", re.S)


@lru_cache(maxsize=1024)
def md_html(text: str, code_bg: str) -> str:
    d = QTextDocument()
    d.setMarkdown(text)
    m = _BODY.search(d.toHtml())
    body = m.group(1) if m else html.escape(text)
    body = body.replace("font-family:'monospace';", f"font-family:'monospace'; background-color:{code_bg};")
    body = body.replace("<pre ", '<pre style="white-space: pre-wrap;" ')   # lange Code-Zeilen umbrechen
    return _PRE_RUN.sub(
        lambda mm: f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{code_bg}" '
                   f'style="margin-top:4px; margin-bottom:6px;"><tr><td>{mm.group(1)}</td></tr></table>',
        body)


def _gap(px=14):
    return f'<p style="margin:0; font-size:2pt; line-height:{px}px;">&nbsp;</p>'


def _label(text, c):
    return f'<p style="margin:0 0 2px 0; color:{c["muted"]}; font-size:8.5pt; font-weight:600;">{text}</p>'


def _reasoning(text, c, tail=False):
    t = text.strip()
    t = ("… " + t[-400:]) if tail and len(t) > 400 else (t[:300] + " …" if len(t) > 300 else t)
    return (f'<p style="margin:2px 0 4px 0; color:{c["muted"]}; font-size:9pt; font-style:italic;">'
            f'💭 {html.escape(t)}</p>')


def _tool_chip(call, result, c):
    f = call["function"]
    line = f'🔧 <b>{html.escape(f["name"])}</b> <span style="color:{c["muted"]}">' \
           f'{html.escape(short_args(f["arguments"], 90))}</span>'
    if result is None:
        line += f' <span style="color:{c["muted"]}">… läuft</span>'
    else:
        first = (result.strip().splitlines() or ["(leer)"])[0][:110]
        line += (f'<br><span style="color:{c["muted"]}">↳ {html.escape(first)} '
                 f'· {len(result)} Zeichen</span>')
    return (f'<table width="100%" cellpadding="7" cellspacing="0" bgcolor="{c["chip"]}" '
            f'style="margin-top:4px; margin-bottom:4px;"><tr><td style="font-size:9pt;">{line}</td></tr></table>')


def render_html(messages, live, c, compact=False) -> str:
    results = {m.get("tool_call_id"): m.get("content") or "" for m in messages if m["role"] == "tool"}
    out, prev = [], None
    side = "4%" if compact else "18%"

    def assistant_open():
        if prev in (None, "user"):
            out.append(_gap())
            out.append(_label("ASSISTENT", c))

    for m in messages:
        r = m["role"]
        if r == "user":
            out.append(_gap(18))
            out.append(
                f'<table width="100%" cellspacing="0" cellpadding="0"><tr><td width="{side}"></td>'
                f'<td><table width="100%" cellpadding="11" cellspacing="0" bgcolor="{c["user"]}"><tr><td>'
                f'{md_html(m.get("content") or "", c["code"])}</td></tr></table></td></tr></table>')
        elif r == "assistant":
            assistant_open()
            if m.get("_reasoning"):
                out.append(_reasoning(m["_reasoning"], c))
            if m.get("content"):
                out.append(md_html(m["content"], c["code"]))
            for tc in m.get("tool_calls") or []:
                out.append(_tool_chip(tc, results.get(tc["id"]), c))
        else:
            continue
        prev = r

    if live is not None:
        assistant_open()
        if live.get("reasoning"):
            out.append(_reasoning(live["reasoning"], c, tail=True))
        content = live.get("content") or ""
        if content:
            out.append(md_html(content + " ▍", c["code"]))
        elif not live.get("reasoning"):
            out.append(f'<p style="color:{c["muted"]}">● ● ●</p>')
    out.append(_gap(10))
    return f'<div style="color:{c["text"]};">{"".join(out)}</div>'


def welcome_html(c) -> str:
    return (f'<table width="100%" height="100%"><tr><td align="center" valign="middle">'
            f'<p style="font-size:22pt; font-weight:700; color:{c["text"]}; margin-top:120px;">Wie kann ich helfen?</p>'
            f'<p style="color:{c["muted"]};">Server, Modell und Profil oben wählen · Strg+K = Kompakt-Modus</p>'
            f'</td></tr></table>')
