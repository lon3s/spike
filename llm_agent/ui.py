"""Qt-Oberfläche."""
import copy
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import config as C
from .agent import Agent
from .client import get_n_ctx, list_models
from .tools import TOOL_LABELS, TOOL_SPECS, Browser, ToolBox, short_args


# ---------- Darstellung ----------

def _quote(text: str) -> str:
    return "> " + text.replace("\n", "\n> ")


def render(messages, live) -> str:
    parts, prev = [], None
    for m in messages:
        r = m["role"]
        if r == "user":
            parts.append(f"### 🧑 Du\n\n{m.get('content', '')}")
        elif r == "assistant":
            s = "### 🤖 Assistent\n\n" if prev in (None, "user") else ""
            if m.get("_reasoning"):
                rs = m["_reasoning"].strip()
                s += _quote("💭 " + (rs[:300] + " …" if len(rs) > 300 else rs)) + "\n\n"
            s += m.get("content") or ""
            for tc in m.get("tool_calls") or []:
                f = tc["function"]
                s += f"\n\n> 🔧 **{f['name']}** {short_args(f['arguments']).replace('`', chr(39))}"
            parts.append(s)
        elif r == "tool":
            c = (m.get("content") or "").strip()
            first = c.splitlines()[0][:160] if c else "(leer)"
            parts.append(_quote(f"↳ {first}  *({len(c)} Zeichen)*"))
        prev = r
    if live is not None:
        s = "### 🤖 Assistent\n\n" if prev in (None, "user") else ""
        if live.get("reasoning"):
            s += _quote("💭 " + live["reasoning"][-600:]) + "\n\n"
        s += (live.get("content") or "") + " ▍"
        parts.append(s)
    return "\n\n".join(parts)


class InputBox(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


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
            for name in TOOL_SPECS:
                cb = QCheckBox(TOOL_LABELS.get(name, name))
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
        f.addRow("Max. Zeichen pro Werkzeug-Ausgabe", self.sp_out)
        f.addRow("Letzte N Werkzeug-Ausgaben voll behalten", self.sp_keep)
        f.addRow("Ältere Werkzeug-Ausgaben kürzen auf (Zeichen)", self.sp_old)
        f.addRow("Max. Werkzeug-Schritte pro Anfrage", self.sp_steps)
        f.addRow("Kontextgröße, falls Server sie nicht meldet", self.sp_ctx)
        f.addRow(self.cb_head)
        f.addRow(QLabel(f"Konfigurationsdatei: {C.CONFIG_FILE}"))
        tabs.addTab(gen, "Allgemein")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)

        self._bridge = Bridge()
        self._bridge.models_loaded.connect(self._test_done)

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
                 default_n_ctx=self.sp_ctx.value(), browser_headless=self.cb_head.isChecked())
        return self.cfg


# ---------- Hauptfenster ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LLM Agent")
        self.resize(1200, 800)
        self.cfg = C.load_config()
        self.browser = Browser()
        self.toolbox = ToolBox(self.cfg["settings"], self.browser)
        self.agent = None
        self.n_ctx = {}          # endpoint-name -> n_ctx
        self.chat = None
        self.bridge = Bridge()
        self.bridge.models_loaded.connect(self._models_loaded)

        # Seitenleiste
        self.chat_list = QListWidget()
        self.chat_list.currentItemChanged.connect(self._chat_selected)
        btn_new = QPushButton("+ Neuer Chat")
        btn_new.clicked.connect(self.new_chat)
        btn_del = QPushButton("Chat löschen")
        btn_del.clicked.connect(self.delete_chat)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.addWidget(btn_new)
        sl.addWidget(self.chat_list)
        sl.addWidget(btn_del)

        # Kopfzeile
        self.cb_ep = QComboBox()
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)
        self.cb_model.setMinimumWidth(240)
        self.cb_profile = QComboBox()
        btn_ref = QPushButton("⟳")
        btn_ref.setToolTip("Modelle neu laden")
        btn_ref.clicked.connect(self.refresh_models)
        btn_set = QPushButton("⚙ Einstellungen")
        btn_set.clicked.connect(self.open_settings)
        top = QHBoxLayout()
        for lbl, w in (("Server:", self.cb_ep), ("Modell:", self.cb_model), ("Profil:", self.cb_profile)):
            top.addWidget(QLabel(lbl))
            top.addWidget(w)
        top.addWidget(btn_ref)
        top.addStretch(1)
        top.addWidget(btn_set)
        self.cb_ep.currentTextChanged.connect(self._ep_changed)
        self.cb_model.currentTextChanged.connect(self._sync_chat_settings)
        self.cb_profile.currentTextChanged.connect(self._sync_chat_settings)

        # Chat
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.input = InputBox()
        self.input.setPlaceholderText("Nachricht … (Enter = senden, Shift+Enter = neue Zeile)")
        self.input.setFixedHeight(100)
        self.input.submitted.connect(self.send)
        self.btn_send = QPushButton("Senden")
        self.btn_send.clicked.connect(self.send)
        self.btn_stop = QPushButton("Stopp")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        btns = QVBoxLayout()
        btns.addWidget(self.btn_send)
        btns.addWidget(self.btn_stop)
        inrow = QHBoxLayout()
        inrow.addWidget(self.input, 1)
        inrow.addLayout(btns)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.addLayout(top)
        ml.addWidget(self.view, 1)
        ml.addLayout(inrow)

        split = QSplitter()
        split.addWidget(side)
        split.addWidget(main)
        split.setSizes([230, 970])
        self.setCentralWidget(split)

        # Statusleiste
        self.status = QLabel()
        self.ctx_bar = QProgressBar()
        self.ctx_bar.setMaximumWidth(260)
        self.ctx_bar.setFormat("Kontext: %p%")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.ctx_bar)

        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+N"))
        act.triggered.connect(self.new_chat)
        self.addAction(act)

        self._fill_combos()
        self._load_chat_list()
        if self.chat_list.count() == 0:
            self.new_chat()
        else:
            self.chat_list.setCurrentRow(0)

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
        self.cb_model.addItems(models)
        if want:
            self.cb_model.setCurrentText(want)
        self.cb_model.blockSignals(False)
        self._sync_chat_settings()
        ctx = n_ctx or self.cfg["settings"]["default_n_ctx"]
        self.status.setText(f"✅ {name}: {len(models)} Modell(e), Kontext {ctx}")

    def _sync_chat_settings(self, *_):
        if self.chat is not None:
            self.chat["endpoint"] = self.cb_ep.currentText()
            self.chat["model"] = self.cb_model.currentText()
            self.chat["profile"] = self.cb_profile.currentText()

    # --- Chats ---
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
        self.chat = C.new_chat(self.cb_ep.currentText(), self.cb_model.currentText(), self.cb_profile.currentText())
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
        cid = cur.data(Qt.UserRole)
        try:
            self.chat = C.load_chat(cid)
        except FileNotFoundError:
            return   # neuer, noch ungespeicherter Chat
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
        self.chat_list.takeItem(self.chat_list.row(it))
        if self.chat_list.count() == 0:
            self.new_chat()

    # --- Senden ---
    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.agent:
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
        self.agent = Agent(self.chat, ep, self.cb_model.currentText(), self.profile(),
                           self.cfg["settings"], self.toolbox, n_ctx)
        self.agent.changed.connect(self.redraw)
        self.agent.stats.connect(self._stats)
        self.agent.error.connect(lambda e: QMessageBox.warning(self, "Fehler", e))
        self.agent.info.connect(self.status.setText)
        self.agent.confirm_request.connect(self._confirm)
        self.agent.finished.connect(self._finished)
        self.btn_send.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.redraw()
        self.agent.start()

    def stop(self):
        if self.agent:
            self.agent.stop()
            self.status.setText("Abbruch …")

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
        self.btn_send.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.redraw()
        self.input.setFocus()

    def redraw(self):
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 30
        live = self.agent.live if self.agent else None
        self.view.setMarkdown(render(self.chat["messages"] if self.chat else [], live))
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
            self.refresh_models()

    def closeEvent(self, e):
        if self.agent:
            self.agent.stop()
        if self.chat and self.chat["messages"]:
            C.save_chat(self.chat)
        self.browser.shutdown()
        super().closeEvent(e)


def main():
    import sys
    app = QApplication(sys.argv)
    app.setApplicationName("llm-agent")
    app.setDesktopFileName("llm-agent")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
