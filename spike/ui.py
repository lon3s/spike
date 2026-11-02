"""Qt-Oberfläche."""
import copy
import threading
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import config as C
from .agent import Agent
from .client import get_n_ctx, list_models
from .theme import THEMES, palette, qss, render_html, welcome_html
from . import plugins
from .tools import Browser, ToolBox, all_tools, load_plugins


class InputBox(QPlainTextEdit):
    """Wächst mit dem Text (bis max. Höhe). Enter = senden, Shift+Enter = Zeilenumbruch."""
    submitted = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Input")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().contentsChanged.connect(self._fit)
        self._fit()

    def _fit(self):
        lines = max(1, int(self.document().size().height()))
        line_h = self.fontMetrics().lineSpacing()
        want = lines * line_h + 18
        self.setFixedHeight(min(220, want))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if want > 220 else Qt.ScrollBarAlwaysOff)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class ChatView(QTextBrowser):
    """Chatverlauf als mittige Spalte mit begrenzter Breite."""
    MAX_W = 860

    def __init__(self):
        super().__init__()
        self.setObjectName("Chat")
        self.setOpenExternalLinks(True)
        self.document().setDocumentMargin(18)

    def resizeEvent(self, e):
        side = max(0, (self.width() - self.MAX_W) // 2)
        self.setViewportMargins(side, 0, side, 0)
        super().resizeEvent(e)


class Bridge(QObject):
    models_loaded = Signal(str, list, int, str)   # endpoint, models, n_ctx, error


# ---------- Einstellungen ----------

class ListEditor(QWidget):
    """Liste links, Formular rechts – für Endpunkte und Profile."""

    def __init__(self, items, new_item, build_form):
        super().__init__()
        self.items, self.new_item = items, new_item
        self.list = QListWidget()
        add, rem = QPushButton("+ Neu"), QPushButton("− Entfernen")
        add.clicked.connect(self._add)
        rem.clicked.connect(self._remove)
        left = QVBoxLayout()
        left.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(rem)
        left.addLayout(row)
        self.form = QWidget()
        self.fields = build_form(self.form, self._changed)
        lay = QHBoxLayout(self)
        lw = QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(220)
        lay.addWidget(lw)
        lay.addWidget(self.form, 1)
        self.list.currentRowChanged.connect(self._load)
        self._loading = False
        self._refresh()

    def _refresh(self, select=0):
        self.list.clear()
        for it in self.items:
            self.list.addItem(it.get("name", "?"))
        self.form.setEnabled(bool(self.items))
        if self.items:
            self.list.setCurrentRow(min(select, len(self.items) - 1))

    def _add(self):
        self.items.append(self.new_item())
        self._refresh(len(self.items) - 1)

    def _remove(self):
        r = self.list.currentRow()
        if r >= 0:
            del self.items[r]
            self._refresh(max(0, r - 1))

    def current(self):
        r = self.list.currentRow()
        return self.items[r] if 0 <= r < len(self.items) else None

    def _load(self, row):
        it = self.current()
        if not it:
            return
        self._loading = True
        for key, (w, get, set_) in self.fields.items():
            set_(it.get(key))
        self._loading = False

    def _changed(self):
        it = self.current()
        if self._loading or not it:
            return
        for key, (w, get, set_) in self.fields.items():
            it[key] = get()
        self.list.currentItem().setText(it.get("name", "?"))


def _line(form, key, label, changed, fields, echo=None):
    w = QLineEdit()
    if echo:
        w.setEchoMode(echo)
    w.textChanged.connect(changed)
    form.addRow(label, w)
    fields[key] = (w, w.text, lambda v, w=w: w.setText(v or ""))
    return w


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.resize(820, 560)
        self.cfg = copy.deepcopy(cfg)
        tabs = QTabWidget()

        # Endpunkte
        def ep_form(w, changed):
            f, fields = QFormLayout(w), {}
            _line(f, "name", "Name", changed, fields)
            url = _line(f, "url", "URL", changed, fields)
            url.setPlaceholderText("http://192.168.1.20:8080  (oder https://api.openai.com)")
            _line(f, "api_key", "API-Key", changed, fields, QLineEdit.Password)
            m = _line(f, "model", "Standard-Modell", changed, fields)
            m.setPlaceholderText("leer = erstes Modell des Servers")
            test = QPushButton("Verbindung testen")
            self.test_label = QLabel()
            self.test_label.setWordWrap(True)
            test.clicked.connect(self._test)
            f.addRow(test, self.test_label)
            hint = QLabel("Jede OpenAI-kompatible API funktioniert: llama.cpp (llama-server), Ollama "
                          "(…:11434), LM Studio, vLLM, OpenAI, OpenRouter … – \"/v1\" wird automatisch ergänzt.")
            hint.setWordWrap(True)
            f.addRow(hint)
            return fields

        self.ep_editor = ListEditor(
            self.cfg["endpoints"],
            lambda: {"name": f"PC{len(self.cfg['endpoints']) + 1}", "url": "http://", "api_key": "", "model": ""},
            ep_form)
        tabs.addTab(self.ep_editor, "Endpunkte / APIs")

        # Profile
        def pr_form(w, changed):
            f, fields = QFormLayout(w), {}
            _line(f, "name", "Name", changed, fields)
            sp = QPlainTextEdit()
            sp.setPlaceholderText("System-Prompt – {date} wird durch das heutige Datum ersetzt")
            sp.textChanged.connect(changed)
            f.addRow("System-Prompt", sp)
            fields["system_prompt"] = (sp, sp.toPlainText, lambda v: sp.setPlainText(v or ""))
            t = QDoubleSpinBox()
            t.setRange(0, 2)
            t.setSingleStep(0.1)
            t.valueChanged.connect(changed)
            f.addRow("Temperatur", t)
            fields["temperature"] = (t, t.value, lambda v: t.setValue(float(v if v is not None else 0.7)))
            mt = QSpinBox()
            mt.setRange(0, 1_000_000)
            mt.setSpecialValueText("unbegrenzt")
            mt.valueChanged.connect(changed)
            f.addRow("Max. Antwort-Tokens", mt)
            fields["max_tokens"] = (mt, mt.value, lambda v: mt.setValue(int(v or 0)))
            boxes = {}
            grid = QWidget()
            g = QVBoxLayout(grid)
            g.setContentsMargins(0, 0, 0, 0)
            for name, label in all_tools():
                cb = QCheckBox(label)
                cb.toggled.connect(changed)
                g.addWidget(cb)
                boxes[name] = cb

            def set_tools(v):
                for n, cb in boxes.items():
                    cb.setChecked(n in (v or []))
            f.addRow("Werkzeuge\n(nur gewählte\nkosten Kontext)", grid)
            fields["tools"] = (grid, lambda: [n for n, cb in boxes.items() if cb.isChecked()], set_tools)
            return fields

        self.pr_editor = ListEditor(
            self.cfg["profiles"],
            lambda: {"name": "Neues Profil", "system_prompt": "", "tools": [], "temperature": 0.7, "max_tokens": 0},
            pr_form)
        tabs.addTab(self.pr_editor, "Profile / System-Prompts")

        # Allgemein
        gen = QWidget()
        f = QFormLayout(gen)
        s = self.cfg["settings"]
        self.sp_out = QSpinBox(); self.sp_out.setRange(500, 200_000); self.sp_out.setValue(s["tool_output_max_chars"])
        self.sp_keep = QSpinBox(); self.sp_keep.setRange(0, 50); self.sp_keep.setValue(s["keep_full_tool_results"])
        self.sp_old = QSpinBox(); self.sp_old.setRange(50, 20_000); self.sp_old.setValue(s["old_tool_result_chars"])
        self.sp_steps = QSpinBox(); self.sp_steps.setRange(1, 100); self.sp_steps.setValue(s["max_steps"])
        self.sp_ctx = QSpinBox(); self.sp_ctx.setRange(512, 2_000_000); self.sp_ctx.setValue(s["default_n_ctx"])
        self.cb_head = QCheckBox("Browser unsichtbar (headless)"); self.cb_head.setChecked(s["browser_headless"])
        self.cb_theme = QComboBox(); self.cb_theme.addItems(["system", "dark", "light"])
        self.cb_theme.setCurrentText(s.get("theme", "system"))
        f.addRow("Design", self.cb_theme)
        f.addRow("Max. Zeichen pro Werkzeug-Ausgabe", self.sp_out)
        f.addRow("Letzte N Werkzeug-Ausgaben voll behalten", self.sp_keep)
        f.addRow("Ältere Werkzeug-Ausgaben kürzen auf (Zeichen)", self.sp_old)
        f.addRow("Max. Werkzeug-Schritte pro Anfrage", self.sp_steps)
        f.addRow("Kontextgröße, falls Server sie nicht meldet", self.sp_ctx)
        f.addRow(self.cb_head)
        f.addRow(QLabel(f"Konfigurationsdatei: {C.CONFIG_FILE}"))
        prow = QHBoxLayout()
        b_open = QPushButton("Plugin-Ordner öffnen")
        b_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins.PLUGIN_DIR))))
        b_reload = QPushButton("Plugins neu laden")
        b_reload.clicked.connect(self._reload_plugins)
        prow.addWidget(b_open)
        prow.addWidget(b_reload)
        prow.addStretch(1)
        f.addRow("Plugins", prow)
        self.plugin_label = QLabel()
        self.plugin_label.setWordWrap(True)
        self.plugin_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(self.plugin_label)
        self._show_plugins()
        tabs.addTab(gen, "Allgemein")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)

        self._bridge = Bridge()
        self._bridge.models_loaded.connect(self._test_done)

    def _show_plugins(self):
        names = ", ".join(plugins.PLUGINS) or "keine"
        txt = f"Ordner: {plugins.PLUGIN_DIR}\nGeladen: {names}"
        if plugins.ERRORS:
            txt += "\n⚠ Fehler:\n" + "\n".join(plugins.ERRORS)
        self.plugin_label.setText(txt)

    def _reload_plugins(self):
        load_plugins()
        self._show_plugins()
        self.plugin_label.setText(self.plugin_label.text() +
                                  "\n(Neue Werkzeuge erscheinen in den Profilen nach erneutem Öffnen der Einstellungen.)")

    def _test(self):
        ep = self.ep_editor.current()
        if not ep:
            return
        self.test_label.setText("teste …")

        def job(ep=dict(ep)):
            try:
                models = list_models(ep)
                self._bridge.models_loaded.emit(ep["name"], models, get_n_ctx(ep) or 0, "")
            except Exception as e:
                self._bridge.models_loaded.emit(ep["name"], [], 0, str(e))
        threading.Thread(target=job, daemon=True).start()

    def _test_done(self, name, models, n_ctx, err):
        if err:
            self.test_label.setText(f"❌ {err}")
        else:
            ctx = f", Kontext {n_ctx}" if n_ctx else ""
            self.test_label.setText(f"✅ {len(models)} Modell(e){ctx}: {', '.join(models[:5])}")

    def result_config(self):
        s = self.cfg["settings"]
        s.update(tool_output_max_chars=self.sp_out.value(), keep_full_tool_results=self.sp_keep.value(),
                 old_tool_result_chars=self.sp_old.value(), max_steps=self.sp_steps.value(),
                 default_n_ctx=self.sp_ctx.value(), browser_headless=self.cb_head.isChecked(),
                 theme=self.cb_theme.currentText())
        return self.cfg


# ---------- Hauptfenster ----------

def _icon_btn(text, tip, slot):
    b = QPushButton(text)
    b.setObjectName("Icon")
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(slot)
    return b


class MainWindow(QMainWindow):
    def __init__(self, popup=False, ctrl=None):
        super().__init__()
        self.popup, self.ctrl = popup, ctrl
        self._hidden_at = 0.0
        self.setWindowTitle("spike")
        self.resize(1200, 820)
        self.cfg = C.load_config()
        loaded, errors = (list(plugins.PLUGINS), list(plugins.ERRORS)) if ctrl else load_plugins()
        self.browser = ctrl.browser if ctrl else Browser()
        self.toolbox = ToolBox(self.cfg["settings"], self.browser)
        self.agent = None
        self.n_ctx = {}          # endpoint-name -> n_ctx
        self.chat = None
        self.colors = THEMES["dark"]
        self.bridge = Bridge()
        self.bridge.models_loaded.connect(self._models_loaded)

        # --- Seitenleiste ---
        self.side = QWidget()
        self.side.setObjectName("Side")
        self.side.setFixedWidth(250)
        title = QLabel("✦  spike")
        title.setStyleSheet("font-size: 13pt; font-weight: 700; padding: 4px 2px;")
        btn_new = QPushButton("+  Neuer Chat")
        btn_new.setObjectName("Primary")
        btn_new.setCursor(Qt.PointingHandCursor)
        btn_new.clicked.connect(self.new_chat)
        self.chat_list = QListWidget()
        self.chat_list.setObjectName("Chats")
        self.chat_list.currentItemChanged.connect(self._chat_selected)
        btn_del = QPushButton("Chat löschen")
        btn_del.setObjectName("Danger")
        btn_del.clicked.connect(self.delete_chat)
        btn_set = QPushButton("⚙  Einstellungen")
        btn_set.clicked.connect(self.open_settings)
        sl = QVBoxLayout(self.side)
        sl.setContentsMargins(12, 14, 12, 12)
        sl.setSpacing(8)
        sl.addWidget(title)
        sl.addWidget(btn_new)
        hist = QLabel("VERLAUF")
        hist.setObjectName("Muted")
        hist.setStyleSheet("font-size: 8.5pt; font-weight: 600; padding: 8px 4px 0 4px;")
        sl.addWidget(hist)
        sl.addWidget(self.chat_list, 1)
        sl.addWidget(btn_del)
        sl.addWidget(btn_set)

        # --- Kopfzeile ---
        self.topbar = QWidget()
        self.topbar.setObjectName("TopBar")
        self.cb_ep = QComboBox()
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)
        self.cb_model.setMinimumWidth(220)
        self.cb_model.setMaximumWidth(380)
        self.cb_profile = QComboBox()
        for cb, tip in ((self.cb_ep, "Server / API"), (self.cb_model, "Modell"), (self.cb_profile, "Profil")):
            cb.setToolTip(tip)
        top = QHBoxLayout(self.topbar)
        top.setContentsMargins(10, 8, 10, 8)
        top.setSpacing(8)
        top.addWidget(_icon_btn("☰", "Seitenleiste ein/aus (Strg+B)", self.toggle_side))
        top.addWidget(self.cb_ep)
        top.addWidget(self.cb_model, 1)
        top.addWidget(self.cb_profile)
        top.addWidget(_icon_btn("⟳", "Modelle neu laden", self.refresh_models))
        top.addStretch(0)
        top.addWidget(_icon_btn("◐", "Hell/Dunkel umschalten", self.toggle_theme))
        top.addWidget(_icon_btn("⤡", "Kompakt-Modus (Strg+K)", self.toggle_compact))
        self.cb_ep.currentTextChanged.connect(self._ep_changed)
        self.cb_model.currentTextChanged.connect(self._sync_chat_settings)
        self.cb_profile.currentTextChanged.connect(self._sync_chat_settings)

        # --- Chat + Eingabe ---
        self.view = ChatView()
        self.input = InputBox()
        self.input.setPlaceholderText("Nachricht schreiben …")
        self.input.setToolTip("Enter = senden · Shift+Enter = neue Zeile · Esc = Stopp")
        self.input.submitted.connect(self.send)
        self.btn_send = QPushButton("➤")
        self.btn_send.setObjectName("Primary")
        self.btn_send.setToolTip("Senden (Enter)")
        self.btn_send.setFixedSize(40, 36)
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.clicked.connect(self.send)
        self.btn_stop = QPushButton("■")
        self.btn_stop.setToolTip("Stopp")
        self.btn_stop.setFixedSize(40, 36)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.hide()
        self.btn_compact = _icon_btn("⤢", "Kompakt-Modus beenden (Strg+K)", self.toggle_compact)
        self.btn_compact.hide()

        composer = QWidget()
        composer.setObjectName("Composer")
        composer.setMaximumWidth(ChatView.MAX_W)
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(10, 6, 6, 6)
        cl.addWidget(self.btn_compact, 0, Qt.AlignBottom)
        cl.addWidget(self.input, 1)
        cl.addWidget(self.btn_stop, 0, Qt.AlignBottom)
        cl.addWidget(self.btn_send, 0, Qt.AlignBottom)
        crow = QHBoxLayout()
        crow.setContentsMargins(16, 4, 16, 12)
        crow.addStretch(1)
        crow.addWidget(composer, 100)
        crow.addStretch(1)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(self.topbar)
        ml.addWidget(self.view, 1)
        ml.addLayout(crow)

        root = QWidget()
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.side)
        rl.addWidget(main, 1)
        self.setCentralWidget(root)

        # --- Statusleiste ---
        self.status = QLabel()
        self.ctx_bar = QProgressBar()
        self.ctx_bar.setMaximumWidth(200)
        self.ctx_bar.setFixedHeight(14)
        self.ctx_bar.setFormat("Kontext %p%")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.ctx_bar)
        self.statusBar().setSizeGripEnabled(False)

        for keys, slot in (("Ctrl+N", self.new_chat), ("Ctrl+K", self.toggle_compact),
                           ("Ctrl+B", self.toggle_side), ("Escape", self.escape)):
            act = QAction(self)
            act.setShortcut(QKeySequence(keys))
            act.triggered.connect(slot)
            self.addAction(act)

        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())
        self.apply_theme()
        self._fill_combos()
        self._load_chat_list()
        if self.chat_list.count() == 0:
            self.new_chat()
        else:
            self.chat_list.setCurrentRow(0)
        self.apply_compact()
        if errors:
            self.status.setText("⚠ Plugin-Fehler: " + " | ".join(errors))
        elif loaded:
            self.status.setText(f"Plugins geladen: {', '.join(loaded)}")

    # --- Design / Kompakt ---
    def apply_theme(self):
        mode = self.cfg["settings"].get("theme", "system")
        if mode == "system":
            dark = QApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
            mode = "dark" if dark else "light"
        self.colors = THEMES[mode]
        app = QApplication.instance()
        app.setPalette(palette(self.colors))
        app.setStyleSheet(qss(self.colors))
        if self.chat is not None:
            self.redraw()

    def toggle_theme(self):
        self.cfg["settings"]["theme"] = "light" if self.colors is THEMES["dark"] else "dark"
        C.save_config(self.cfg)
        self.apply_theme()

    def is_compact(self):
        return self.popup or bool(self.cfg["settings"].get("compact"))

    def apply_compact(self):
        compact = self.is_compact()
        self.side.setVisible(not compact and self.cfg["settings"].get("show_side", True))
        self.topbar.setVisible(not compact)
        self.statusBar().setVisible(not compact)
        self.btn_compact.setVisible(compact)
        self.redraw()

    def toggle_compact(self):
        if self.popup:      # im Popup: großes Fenster mit demselben Chat öffnen
            if self.ctrl:
                self.ctrl.show_main(self.chat["id"] if self.chat else None)
            return
        self.cfg["settings"]["compact"] = not self.cfg["settings"].get("compact")
        C.save_config(self.cfg)
        self.apply_compact()
        self.input.setFocus()

    def escape(self):
        if self.agent:
            self.stop()
        elif self.popup:
            self.hide()

    def toggle_side(self):
        if self.is_compact():
            return
        self.cfg["settings"]["show_side"] = not self.cfg["settings"].get("show_side", True)
        C.save_config(self.cfg)
        self.side.setVisible(self.cfg["settings"]["show_side"])

    # --- Kombos ---
    def _fill_combos(self):
        for cb, items in ((self.cb_ep, self.cfg["endpoints"]), (self.cb_profile, self.cfg["profiles"])):
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems([i["name"] for i in items])
            if cur:
                cb.setCurrentText(cur)
            cb.blockSignals(False)

    def endpoint(self):
        return next((e for e in self.cfg["endpoints"] if e["name"] == self.cb_ep.currentText()), None)

    def profile(self):
        return next((p for p in self.cfg["profiles"] if p["name"] == self.cb_profile.currentText()),
                    self.cfg["profiles"][0] if self.cfg["profiles"] else {})

    def _ep_changed(self, _):
        self._sync_chat_settings()
        self.refresh_models()

    def refresh_models(self):
        ep = self.endpoint()
        if not ep:
            return
        self.status.setText(f"Verbinde mit {ep['name']} …")

        def job(ep=dict(ep)):
            try:
                models = list_models(ep)
                self.bridge.models_loaded.emit(ep["name"], models, get_n_ctx(ep) or 0, "")
            except Exception as e:
                self.bridge.models_loaded.emit(ep["name"], [], 0, str(e))
        threading.Thread(target=job, daemon=True).start()

    def _models_loaded(self, name, models, n_ctx, err):
        if name != self.cb_ep.currentText():
            return
        if err:
            self.status.setText(f"❌ {name}: {err}")
            return
        if n_ctx:
            self.n_ctx[name] = n_ctx
        want = (self.chat or {}).get("model") or (self.endpoint() or {}).get("model") or ""
        self.cb_model.blockSignals(True)
        self.cb_model.clear()
        for mid in models:   # lange Pfade kurz anzeigen, volle ID als Daten
            self.cb_model.addItem(mid.rsplit("/", 1)[-1], mid)
        idx = self.cb_model.findData(want)
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        elif want:
            self.cb_model.setEditText(want)
        self.cb_model.blockSignals(False)
        self._sync_chat_settings()
        ctx = n_ctx or self.cfg["settings"]["default_n_ctx"]
        self.status.setText(f"●  {name} verbunden · {len(models)} Modell(e) · Kontext {ctx:,}".replace(",", "."))

    def model_id(self):
        i = self.cb_model.currentIndex()
        data = self.cb_model.itemData(i) if i >= 0 else None
        text = self.cb_model.currentText()
        return data if data and self.cb_model.itemText(i) == text else text

    def _sync_chat_settings(self, *_):
        if self.chat is not None:
            self.chat["endpoint"] = self.cb_ep.currentText()
            self.chat["model"] = self.model_id()
            self.chat["profile"] = self.cb_profile.currentText()

    # --- Chats ---
    def select_chat(self, chat_id):
        """Chatliste neu einlesen und einen Chat öffnen (z. B. aus dem Popup übernommen)."""
        if self.agent:
            return
        self._load_chat_list()
        for i in range(self.chat_list.count()):
            if self.chat_list.item(i).data(Qt.UserRole) == chat_id:
                self.chat_list.setCurrentRow(i)
                self.chat = C.load_chat(chat_id)
                self.redraw(force_bottom=True)
                return
        if self.chat_list.count():
            self.chat_list.setCurrentRow(0)

    def _load_chat_list(self):
        self.chat_list.blockSignals(True)
        self.chat_list.clear()
        for c in C.list_chats():
            it = QListWidgetItem(c["title"])
            it.setData(Qt.UserRole, c["id"])
            self.chat_list.addItem(it)
        self.chat_list.blockSignals(False)

    def new_chat(self):
        if self.agent:
            return
        if self.chat is not None and not self.chat["messages"]:
            self.input.setFocus()   # leerer Chat existiert schon
            return
        self.chat = C.new_chat(self.cb_ep.currentText(), self.model_id(), self.cb_profile.currentText())
        it = QListWidgetItem(self.chat["title"])
        it.setData(Qt.UserRole, self.chat["id"])
        self.chat_list.blockSignals(True)
        self.chat_list.insertItem(0, it)
        self.chat_list.setCurrentRow(0)
        self.chat_list.blockSignals(False)
        self.ctx_bar.setValue(0)
        self.redraw()
        self.input.setFocus()

    def _chat_selected(self, cur, prev):
        if not cur:
            return
        if self.agent:   # während einer Antwort nicht wechseln
            self.chat_list.blockSignals(True)
            self.chat_list.setCurrentItem(prev)
            self.chat_list.blockSignals(False)
            return
        try:
            self.chat = C.load_chat(cur.data(Qt.UserRole))
        except FileNotFoundError:
            return   # neuer, noch ungespeicherter Chat
        if prev and prev.text() == "Neuer Chat" and self.chat_list.row(prev) >= 0:
            self.chat_list.takeItem(self.chat_list.row(prev))   # leere Chats aufräumen
        for cb, key in ((self.cb_ep, "endpoint"), (self.cb_profile, "profile")):
            if self.chat.get(key) and cb.findText(self.chat[key]) >= 0:
                cb.blockSignals(True)
                cb.setCurrentText(self.chat[key])
                cb.blockSignals(False)
        self.ctx_bar.setValue(0)
        self.refresh_models()
        self.redraw()

    def delete_chat(self):
        it = self.chat_list.currentItem()
        if not it or self.agent:
            return
        if QMessageBox.question(self, "Löschen", f"Chat „{it.text()}“ löschen?") != QMessageBox.Yes:
            return
        C.delete_chat(it.data(Qt.UserRole))
        self.chat = None
        self.chat_list.takeItem(self.chat_list.row(it))
        if self.chat_list.count() == 0:
            self.new_chat()

    # --- Senden ---
    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.agent or self.chat is None:
            return
        ep = self.endpoint()
        if not ep:
            QMessageBox.warning(self, "Kein Server", "Bitte zuerst unter ⚙ Einstellungen einen Endpunkt anlegen.")
            return
        self._sync_chat_settings()
        self.input.clear()
        self.chat["messages"].append({"role": "user", "content": text})
        if self.chat["title"] == "Neuer Chat":
            self.chat["title"] = text.replace("\n", " ")[:45]
            it = self.chat_list.currentItem()
            if it:
                it.setText(self.chat["title"])
        n_ctx = self.n_ctx.get(ep["name"]) or self.cfg["settings"]["default_n_ctx"]
        self.agent = Agent(self.chat, ep, self.model_id(), self.profile(),
                           self.cfg["settings"], self.toolbox, n_ctx)
        self.agent.changed.connect(self.redraw)
        self.agent.stats.connect(self._stats)
        self.agent.error.connect(lambda e: QMessageBox.warning(self, "Fehler", e))
        self.agent.info.connect(self._info)
        self.agent.confirm_request.connect(self._confirm)
        self.agent.finished.connect(self._finished)
        self.btn_send.hide()
        self.btn_stop.show()
        self.redraw(force_bottom=True)
        self.agent.start()

    def stop(self):
        if self.agent:
            self.agent.stop()
            self.status.setText("Abbruch …")

    def _info(self, text):
        self.status.setText(text)
        if self.is_compact():
            self.input.setPlaceholderText(text or "Nachricht schreiben …")

    def _confirm(self, text):
        ok = QMessageBox.question(self, "Bestätigen", text) == QMessageBox.Yes
        if self.agent:
            self.agent.answer(ok)

    def _stats(self, text, used, n_ctx):
        if n_ctx:
            self.ctx_bar.setValue(min(100, int(used * 100 / n_ctx)))
            self.ctx_bar.setToolTip(f"{used} / {n_ctx} Tokens")
        self.status.setText(text)

    def _finished(self):
        C.save_chat(self.chat)
        self.agent = None
        self.btn_stop.hide()
        self.btn_send.show()
        self.input.setPlaceholderText("Nachricht schreiben …")
        self.redraw()
        self.input.setFocus()

    def redraw(self, force_bottom=False):
        sb = self.view.verticalScrollBar()
        at_bottom = force_bottom or sb.value() >= sb.maximum() - 40
        msgs = self.chat["messages"] if self.chat else []
        live = self.agent.live if self.agent else None
        if not msgs and live is None:
            self.view.setHtml(welcome_html(self.colors))
            return
        self.view.setHtml(render_html(msgs, live, self.colors, self.is_compact()))
        if at_bottom:
            sb.setValue(sb.maximum())

    # --- Einstellungen ---
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            new = dlg.result_config()
            self.cfg["endpoints"], self.cfg["profiles"] = new["endpoints"], new["profiles"]
            self.cfg["settings"].update(new["settings"])   # gleiches dict, ToolBox sieht Änderungen
            C.save_config(self.cfg)
            self._fill_combos()
            self.apply_theme()
            self.refresh_models()

    def changeEvent(self, e):
        # Popup schließt sich, sobald man daneben klickt (wie das Lautstärke-Popup)
        if self.popup and e.type() == QEvent.ActivationChange and not self.isActiveWindow():
            QTimer.singleShot(120, self._maybe_autohide)
        super().changeEvent(e)

    def _maybe_autohide(self):
        if self.isVisible() and not self.isActiveWindow() and QApplication.activeModalWidget() is None \
                and QApplication.activeWindow() is None:
            self.hide()

    def hideEvent(self, e):
        self._hidden_at = time.monotonic()
        if self.chat and self.chat["messages"] and not self.agent:
            C.save_chat(self.chat)
        super().hideEvent(e)

    def closeEvent(self, e):
        if self.chat and self.chat["messages"]:
            C.save_chat(self.chat)
        if self.ctrl and self.ctrl.tray_active():
            e.ignore()          # bleibt im Tray
            self.hide()
            return
        if self.agent:
            self.agent.stop()
        super().closeEvent(e)
        if self.ctrl:
            self.ctrl.quit()          # kein Tray vorhanden → Programm beenden
        else:
            self.browser.shutdown()



def main():
    from .tray import main as tray_main
    tray_main()
