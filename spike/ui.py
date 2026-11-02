"""Qt-Oberfläche."""
import copy
import os
from pathlib import Path
import re
import threading
import time

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QConicalGradient, QDesktopServices, QKeySequence, QPainter,
                           QPainterPath, QPen, QRegion)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QColorDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import config as C
from .agent import Agent
from .client import get_n_ctx, has_vision, list_models
from .chatview import ChatView, shadow
from .theme import THEMES, palette, qss
from . import attachments as A, mcp, memory, plugins, scheduler as S, speech
from . import tools as T
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

    key_hook = None     # fn(key) -> True, wenn die Taste verbraucht wurde (Befehlsliste)
    attach_hook = None  # fn(mime) -> True, wenn Bild/Dateien als Anhang übernommen wurden

    def canInsertFromMimeData(self, mime):
        return mime.hasImage() or mime.hasUrls() or super().canInsertFromMimeData(mime)

    def insertFromMimeData(self, mime):
        if self.attach_hook and (mime.hasImage() or (mime.hasUrls() and all(u.isLocalFile() for u in mime.urls()))):
            if self.attach_hook(mime):
                return
        super().insertFromMimeData(mime)

    def keyPressEvent(self, e):
        if self.key_hook and not (e.modifiers() & Qt.ShiftModifier) and self.key_hook(e.key()):
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class Bridge(QObject):
    models_loaded = Signal(str, list, int, str)   # endpoint, models, n_ctx, error
    screenshot_done = Signal(object, str)          # anhang (oder None), fehler
    open_chat = Signal(str)                        # Klick auf „Öffnen“ in einer Benachrichtigung
    task_done = Signal(str, str, str)              # chat_id, titel, antwort (geplante Aufgabe fertig)
    stt_done = Signal(str, str, str)               # text, info, fehler
    speech_state = Signal(str)
    shell_update = Signal()                       # !-Befehl: neue Ausgabe
    shell_done = Signal()


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


class PiperDownloadDialog(QDialog):
    """Piper-Stimmen aus dem offiziellen Katalog auswählen und herunterladen."""
    progress = Signal(int)
    loaded = Signal(object, str)
    finished_dl = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Piper-Stimmen")
        self.resize(640, 520)
        self.installed = ""
        self.catalog = []
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Suchen (Name, Sprache) …")
        self.search.textChanged.connect(self._fill)
        self.only_de = QCheckBox("nur Deutsch")
        self.only_de.setChecked(True)
        self.only_de.toggled.connect(self._fill)
        top.addWidget(self.search, 1)
        top.addWidget(self.only_de)
        lay.addLayout(top)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._download())
        lay.addWidget(self.list, 1)
        self.info = QLabel("Lade Katalog …")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.hide()
        row.addWidget(self.bar, 1)
        self.b_dl = QPushButton("Herunterladen")
        self.b_dl.setObjectName("Primary")
        self.b_dl.clicked.connect(self._download)
        row.addWidget(self.b_dl)
        close = QPushButton("Schließen")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.progress.connect(lambda p: (self.bar.show(), self.bar.setValue(p)))
        self.loaded.connect(self._loaded)
        self.finished_dl.connect(self._done)

        def job():
            try:
                self.loaded.emit(speech.piper_catalog(), "")
            except Exception as e:
                self.loaded.emit([], str(e))
        threading.Thread(target=job, daemon=True).start()

    def _loaded(self, cat, err):
        if err:
            self.info.setText(f"⚠ Katalog nicht erreichbar: {err}")
            return
        self.catalog = sorted(cat, key=lambda v: (not v["lang"].startswith("de"), v["lang"], v["name"],
                                                  ["x_low", "low", "medium", "high"].index(v["quality"])
                                                  if v["quality"] in ("x_low", "low", "medium", "high") else 9))
        self.info.setText("Doppelklick oder „Herunterladen“. Qualität: x_low/low = klein und schnell, "
                          "medium = guter Kompromiss, high = am natürlichsten.")
        self._fill()

    def _fill(self):
        have = {n for n, _ in speech.piper_voices()}
        q = self.search.text().strip().lower()
        self.list.clear()
        for v in self.catalog:
            if self.only_de.isChecked() and not v["lang"].startswith("de"):
                continue
            text = f"{v['lang']} · {v['name']} · {v['quality']} · {v['size'] / 1e6:.0f} MB"
            if v["speakers"] > 1:
                text += f" · {v['speakers']} Sprecher"
            if q and q not in (text + v["lang_name"]).lower():
                continue
            if v["key"] in have:
                text = "✓ " + text + "  (installiert)"
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, v)
            self.list.addItem(it)

    def _download(self):
        it = self.list.currentItem()
        if not it:
            return
        v = it.data(Qt.UserRole)
        self.b_dl.setEnabled(False)
        self.info.setText(f"Lade {v['key']} ({v['size'] / 1e6:.0f} MB) …")

        def job():
            try:
                self.finished_dl.emit(speech.download_piper_voice(v, self.progress.emit), "")
            except Exception as e:
                self.finished_dl.emit("", str(e))
        threading.Thread(target=job, daemon=True).start()

    def _done(self, path, err):
        self.b_dl.setEnabled(True)
        self.bar.hide()
        if err:
            self.info.setText(f"⚠ Download fehlgeschlagen: {err}")
            return
        self.installed = path
        self.info.setText(f"✅ Installiert: {Path(path).name} – ist jetzt ausgewählt.")
        self._fill()


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.resize(960, 620)
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
            th = QComboBox()
            th.addItem("Standard (wie der Server eingestellt ist)", "standard")
            th.addItem("An – gründlicher, aber langsamer", "an")
            th.addItem("Aus – schnell, gut für einfache Aufgaben", "aus")
            th.setToolTip("Ob das Modell vor der Antwort „nachdenkt“ (💭). Wirkt bei llama.cpp mit Qwen & Co.\n"
                          "Im Chat umschalten: /denken an|aus")
            th.currentIndexChanged.connect(changed)
            f.addRow("Denken", th)
            fields["thinking"] = (th, th.currentData,
                                  lambda v: th.setCurrentIndex(max(0, th.findData(v or "standard"))))
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
            cb_all = QCheckBox("Alle Werkzeuge – das Modell entscheidet selbst (auch künftige Plugins)")
            cb_all.setStyleSheet("font-weight: 600;")
            g.addWidget(cb_all)
            for name, label in all_tools():
                cb = QCheckBox(label)
                cb.toggled.connect(changed)
                g.addWidget(cb)
                boxes[name] = cb

            def all_toggled(on):
                for cb in boxes.values():
                    cb.setEnabled(not on)
                    if on:
                        cb.setChecked(True)
                changed()
            cb_all.toggled.connect(all_toggled)

            def set_tools(v):
                v = v or []
                cb_all.blockSignals(True)
                cb_all.setChecked("*" in v)
                cb_all.blockSignals(False)
                for n, cb in boxes.items():
                    cb.setEnabled("*" not in v)
                    cb.setChecked("*" in v or n in v)

            def get_tools():
                return ["*"] if cb_all.isChecked() else [n for n, cb in boxes.items() if cb.isChecked()]
            f.addRow("Werkzeuge\n(nur gewählte\nkosten Kontext)", grid)
            fields["tools"] = (grid, get_tools, set_tools)
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
        self.cb_index = QCheckBox("Dateisuche: Laufwerke unter /mnt täglich im Hintergrund indizieren")
        self.cb_index.setChecked(bool(s.get("drive_index", True)))
        self.cb_index.setToolTip("Macht find_files auf NAS/Laufwerken in unter 1 s möglich. Läuft mit niedrigster "
                                 f"Priorität, Index: {T.DRIVE_INDEX}")
        f.addRow(self.cb_index)
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
        tabs.addTab(self._memory_tab(s), "Gedächtnis && Kontext")
        tabs.addTab(self._mcp_tab(s), "MCP-Server")
        tabs.addTab(self._speech_tab(s), "Sprache")
        tabs.addTab(self._glow_tab(s), "Popup-Rahmen")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(bb)

        self._bridge = Bridge()
        self._bridge.models_loaded.connect(self._test_done)

    def _speech_tab(self, s):
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(QLabel("<b>Sprechen 🎤</b> (Strg+M) – faster-whisper lokal oder ein Server"))
        self.sp_fields = {}

        def line(key, label, ph=""):
            e = QLineEdit(str(s.get(key, "")))
            e.setPlaceholderText(ph)
            f.addRow(label, e)
            self.sp_fields[key] = e.text
            return e
        line("stt_python", "Python mit faster-whisper", f"leer = automatisch ({speech._find_python(s) or 'nicht gefunden'})")
        line("stt_model", "Modell", f"leer = automatisch ({speech._find_model(s)})")
        dev = QComboBox()
        dev.addItems(["auto", "cuda", "cpu"])
        dev.setCurrentText(s.get("stt_device", "auto"))
        dev.setToolTip("auto: GPU, wenn genug Speicher frei ist, sonst CPU")
        f.addRow("Rechnen auf", dev)
        self.sp_fields["stt_device"] = dev.currentText
        line("stt_language", "Sprache", "de")
        line("stt_url", "Oder Server-URL", "z. B. http://192.168.178.185:8080 (OpenAI /v1/audio/transcriptions)")
        auto = QCheckBox("Nach dem Sprechen sofort senden")
        auto.setChecked(bool(s.get("stt_autosend")))
        f.addRow(auto)
        self.sp_fields["stt_autosend"] = auto.isChecked
        f.addRow(QLabel("<b>Vorlesen 🔊</b> – VoxCPM-API (deine Stimme)"))
        line("tts_url", "TTS-URL", "http://127.0.0.1:8000/tts")
        line("tts_voice", "Stimme", "senol1")
        line("tts_language", "Sprache", "de")
        line("tts_start", "Startbefehl (falls nicht läuft)", "leer = nicht automatisch starten")
        tdev = QComboBox()
        tdev.addItem("auto – GPU, wenn genug frei ist, sonst CPU", "auto")
        tdev.addItem("immer GPU", "gpu")
        tdev.addItem("immer CPU (langsam, ~5× Echtzeit)", "cpu")
        tdev.setCurrentIndex(max(0, tdev.findData(s.get("tts_device", "auto"))))
        f.addRow("Rechnen auf", tdev)
        self.sp_fields["tts_device"] = tdev.currentData
        eng = QComboBox()
        eng.addItem("auto – VoxCPM wenn GPU frei, sonst Piper", "auto")
        eng.addItem("immer VoxCPM (deine Stimme)", "voxcpm")
        eng.addItem("immer Piper (schnell, auch auf der CPU)", "piper")
        eng.setCurrentIndex(max(0, eng.findData(s.get("tts_engine", "auto"))))
        f.addRow("Stimme verwenden", eng)
        self.sp_fields["tts_engine"] = eng.currentData
        # Piper-Stimme: Auswahl der installierten, Probehören, weitere herunterladen
        self.piper_cb = QComboBox()
        row = QHBoxLayout()
        row.addWidget(self.piper_cb, 1)
        b_try = QPushButton("▶ Probehören")
        b_try.clicked.connect(self._piper_try)
        row.addWidget(b_try)
        b_dl = QPushButton("Stimmen herunterladen …")
        b_dl.clicked.connect(self._piper_download)
        row.addWidget(b_dl)
        f.addRow("Piper-Stimme", row)
        self.sp_fields["piper_model"] = lambda: self.piper_cb.currentData() or ""
        self.piper_status = QLabel()
        self.piper_status.setObjectName("Muted")
        f.addRow("", self.piper_status)
        self._piper_fill(s.get("piper_model", ""))
        ta = QCheckBox("Antworten automatisch vorlesen")
        ta.setChecked(bool(s.get("tts_auto")))
        f.addRow(ta)
        self.sp_fields["tts_auto"] = ta.isChecked
        hint = QLabel("VoxCPM: auf der GPU schnell, auf der CPU etwa 5× so lang wie gesprochen (gemessen). Wird es "
                      "automatisch gestartet, beendet spike es beim Beenden wieder.")
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        f.addRow(hint)
        return w

    def _piper_fill(self, want=""):
        self.piper_cb.clear()
        voices = speech.piper_voices()
        for name, path in voices:
            self.piper_cb.addItem(name, path)
        idx = self.piper_cb.findData(os.path.expanduser(want)) if want else -1
        self.piper_cb.setCurrentIndex(idx if idx >= 0 else 0)
        if not speech.piper_bin(self.cfg["settings"]):
            self.piper_status.setText("Piper-Programm fehlt: ~/spike/.venv/bin/pip install piper-tts")
        elif not voices:
            self.piper_status.setText("Noch keine Stimme – „Stimmen herunterladen …“")
        else:
            self.piper_status.setText(f"{len(voices)} Stimme(n) installiert in {speech.PIPER_DIR}")

    def _piper_try(self):
        path = self.piper_cb.currentData()
        if not path:
            return
        st = {**self.cfg["settings"], "piper_model": path}
        if not hasattr(self, "_try_speaker"):
            self._try_speaker = speech.Speaker(st, on_state=lambda t: None)
        self._try_speaker.settings = st
        self._try_speaker.speak("Hallo! So klinge ich. Ich lese dir die Antworten von spike vor.", engine="piper")

    def _piper_download(self):
        dlg = PiperDownloadDialog(self)
        dlg.exec()
        self._piper_fill(dlg.installed or self.piper_cb.currentData() or "")

    def _mcp_tab(self, s):
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("MCP-Server sind fertige Werkzeug-Pakete (GitHub, Kalender, Datenbanken, Home Assistant …). "
                       "Befehl eintragen, speichern – die Werkzeuge erscheinen dann in den Profilen "
                       "(im Profil „Alles“ automatisch). Beispiele: <code>uvx mcp-server-time</code>, "
                       "<code>npx -y @modelcontextprotocol/server-filesystem ~/Dokumente</code>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        s.setdefault("mcp_servers", [])

        def form(fw, changed):
            f, fields = QFormLayout(fw), {}
            _line(f, "name", "Name", changed, fields)
            cmd = _line(f, "command", "Befehl", changed, fields)
            cmd.setPlaceholderText("z. B. uvx mcp-server-time")
            env = QPlainTextEdit()
            env.setPlaceholderText("Umgebungsvariablen, eine pro Zeile: GITHUB_TOKEN=…")
            env.setMaximumHeight(80)
            env.textChanged.connect(changed)
            f.addRow("Umgebung", env)

            def get_env():
                out = {}
                for line in env.toPlainText().splitlines():
                    k, sep, v = line.partition("=")
                    if sep and k.strip():
                        out[k.strip()] = v.strip()
                return out
            fields["env"] = (env, get_env, lambda v: env.setPlainText("\n".join(f"{k}={x}" for k, x in (v or {}).items())))
            en = QCheckBox("aktiv")
            en.toggled.connect(changed)
            f.addRow(en)
            fields["enabled"] = (en, en.isChecked, lambda v: en.setChecked(v is None or bool(v)))
            cf = QComboBox()
            cf.addItem("Nur bei Werkzeugen, die etwas ändern (empfohlen)", "schreibend")
            cf.addItem("Immer nachfragen", "immer")
            cf.addItem("Nie nachfragen", "nie")
            cf.currentIndexChanged.connect(changed)
            f.addRow("Freigabe", cf)
            fields["confirm"] = (cf, cf.currentData, lambda v: cf.setCurrentIndex(max(0, cf.findData(v or "schreibend"))))
            return fields
        self.mcp_editor = ListEditor(s["mcp_servers"], lambda: {"name": "neu", "command": "", "env": {},
                                                                 "enabled": True, "confirm": "schreibend"}, form)
        lay.addWidget(self.mcp_editor, 1)
        self.mcp_status = QLabel()
        self.mcp_status.setWordWrap(True)
        self.mcp_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.mcp_status)
        upd = lambda: self.mcp_status.setText("Status: " + (" · ".join(f"{k}: {v}" for k, v in mcp.STATUS.items())
                                                            or "noch keine Server"))
        upd()
        t = QTimer(w)
        t.timeout.connect(upd)
        t.start(1000)
        return w

    def _memory_tab(self, s):
        w = QWidget()
        lay = QVBoxLayout(w)
        f = QFormLayout()
        lay.addLayout(f)
        self.cb_mem = QCheckBox("Gedächtnis benutzen")
        self.cb_mem.setChecked(bool(s.get("memory_enabled", True)))
        f.addRow(self.cb_mem)
        self.sp_mem = QSpinBox()
        self.sp_mem.setRange(0, 20_000)
        self.sp_mem.setSingleStep(250)
        self.sp_mem.setSuffix(" Zeichen")
        self.sp_mem.setValue(int(s.get("memory_prompt_chars", 1500)))
        self.sp_mem.setToolTip("So viel Gedächtnis geht höchstens in jeden Chat (≈ Zeichen / 3,3 Tokens). "
                               "Angeheftete und zur ersten Nachricht passende Einträge kommen zuerst, "
                               "den Rest holt sich das Modell bei Bedarf mit memory_search.")
        f.addRow("Gedächtnis im Prompt höchstens", self.sp_mem)
        self.cb_compact = QCheckBox("Älteren Verlauf automatisch zusammenfassen, wenn der Kontext voll wird")
        self.cb_compact.setChecked(bool(s.get("auto_compact", True)))
        f.addRow(self.cb_compact)
        self.sp_cat = QSpinBox()
        self.sp_cat.setRange(30, 95)
        self.sp_cat.setSuffix(" %")
        self.sp_cat.setValue(int(s.get("compact_at_percent", 70)))
        f.addRow("Zusammenfassen ab Auslastung", self.sp_cat)
        self.sp_ckeep = QSpinBox()
        self.sp_ckeep.setRange(10, 60)
        self.sp_ckeep.setSuffix(" %")
        self.sp_ckeep.setValue(int(s.get("compact_keep_percent", 30)))
        f.addRow("Davon wörtlich behalten (neuester Teil)", self.sp_ckeep)
        self.sp_pyto = QSpinBox()
        self.sp_pyto.setRange(5, 3600)
        self.sp_pyto.setSuffix(" s")
        self.sp_pyto.setValue(int(s.get("python_timeout", 120)))
        f.addRow("Zeitlimit für Python", self.sp_pyto)

        lay.addWidget(QLabel("<b>Einträge</b> (Änderungen gelten sofort) – 📌 = immer im Prompt"))
        self.mem_list = QListWidget()
        self.mem_list.itemDoubleClicked.connect(lambda _: self._mem_edit())
        lay.addWidget(self.mem_list, 1)
        row = QHBoxLayout()
        for text, slot in (("+ Neu", self._mem_add), ("Bearbeiten", self._mem_edit),
                           ("📌 Anheften", self._mem_pin), ("Löschen", self._mem_del)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        self.mem_count = QLabel()
        row.addWidget(self.mem_count)
        lay.addLayout(row)
        self._mem_fill()
        return w

    def _mem_fill(self):
        self.mem_list.clear()
        items = memory.load()
        for m in items:
            it = QListWidgetItem(("📌 " if m.get("pinned") else "") + m["text"])
            it.setData(Qt.UserRole, m["id"])
            it.setToolTip(f"id {m['id']}")
            self.mem_list.addItem(it)
        chars = sum(len(m["text"]) + 10 for m in items)
        self.mem_count.setText(f"{len(items)} Einträge · {chars} Zeichen gesamt")

    def _mem_current(self):
        it = self.mem_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _mem_ask(self, title, text=""):
        from PySide6.QtWidgets import QInputDialog
        val, ok = QInputDialog.getMultiLineText(self, title, f"Ein kurzer Fakt (max. {memory.MAX_FACT} Zeichen):", text)
        return val.strip() if ok else ""

    def _mem_add(self):
        t = self._mem_ask("Neuer Eintrag")
        if t:
            memory.add(t)
            self._mem_fill()

    def _mem_edit(self):
        mid = self._mem_current()
        if not mid:
            return
        old = next((m["text"] for m in memory.load() if m["id"] == mid), "")
        t = self._mem_ask("Eintrag bearbeiten", old)
        if t:
            memory.update(mid, t)
            self._mem_fill()

    def _mem_pin(self):
        mid = self._mem_current()
        if mid:
            m = next((m for m in memory.load() if m["id"] == mid), None)
            if m:
                memory.set_pinned(mid, not m.get("pinned"))
                self._mem_fill()

    def _mem_del(self):
        mid = self._mem_current()
        if mid:
            memory.delete(mid)
            self._mem_fill()

    def _glow_tab(self, s):
        w = QWidget()
        f = QFormLayout(w)
        self.cb_glow = QCheckBox("Leuchtrahmen um das Tray-Popup")
        self.cb_glow.setChecked(bool(s.get("popup_glow", True)))
        f.addRow(self.cb_glow)
        self.cb_glow_style = QComboBox()
        self.cb_glow_style.addItem("Lauflicht (Lichtpunkt mit Schweif)", "lauflicht")
        self.cb_glow_style.addItem("Farbverlauf (ganzer Rahmen, dreht sich)", "verlauf")
        i = self.cb_glow_style.findData(s.get("popup_glow_style", "lauflicht"))
        self.cb_glow_style.setCurrentIndex(max(0, i))
        f.addRow("Stil", self.cb_glow_style)

        self.glow_colors = list(s.get("popup_glow_colors") or ["#8b7cff"])
        self.glow_row = QHBoxLayout()
        crow = QWidget()
        crow.setLayout(self.glow_row)
        self.glow_row.setContentsMargins(0, 0, 0, 0)
        f.addRow("Farben", crow)
        self._glow_buttons()

        self.sp_glow_speed = QDoubleSpinBox()
        self.sp_glow_speed.setRange(0.5, 60)
        self.sp_glow_speed.setSingleStep(0.5)
        self.sp_glow_speed.setSuffix(" s pro Umlauf")
        self.sp_glow_speed.setValue(float(s.get("popup_glow_speed", 4.0)))
        f.addRow("Geschwindigkeit", self.sp_glow_speed)
        self.sp_glow_width = QSpinBox()
        self.sp_glow_width.setRange(1, 8)
        self.sp_glow_width.setSuffix(" px")
        self.sp_glow_width.setValue(int(s.get("popup_glow_width", 2)))
        f.addRow("Breite", self.sp_glow_width)
        hint = QLabel("Das Licht läuft im Uhrzeigersinn um den Rahmen. Farbe anklicken zum Ändern, "
                      "„+“ fügt eine Farbe hinzu, Rechtsklick auf eine Farbe entfernt sie.")
        hint.setWordWrap(True)
        f.addRow(hint)
        return w

    def _glow_buttons(self):
        while self.glow_row.count():
            it = self.glow_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, col in enumerate(self.glow_colors):
            b = QPushButton()
            b.setFixedSize(34, 28)
            b.setToolTip(f"{col} – klicken zum Ändern, Rechtsklick entfernt")
            b.setStyleSheet(f"background: {col}; border: 1px solid #888; border-radius: 8px;")
            b.clicked.connect(lambda _=False, i=i: self._pick_glow(i))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, i=i: self._del_glow(i))
            self.glow_row.addWidget(b)
        add = QPushButton("+")
        add.setFixedSize(34, 28)
        add.setToolTip("Farbe hinzufügen")
        add.setEnabled(len(self.glow_colors) < 8)
        add.clicked.connect(lambda: self._pick_glow(len(self.glow_colors)))
        self.glow_row.addWidget(add)
        self.glow_row.addStretch(1)

    def _pick_glow(self, i):
        start = QColor(self.glow_colors[i] if i < len(self.glow_colors) else "#ffffff")
        col = QColorDialog.getColor(start, self, "Rahmenfarbe")
        if not col.isValid():
            return
        if i < len(self.glow_colors):
            self.glow_colors[i] = col.name()
        else:
            self.glow_colors.append(col.name())
        self._glow_buttons()

    def _del_glow(self, i):
        if len(self.glow_colors) > 1:
            del self.glow_colors[i]
            self._glow_buttons()

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
                 drive_index=self.cb_index.isChecked(),
                 theme=self.cb_theme.currentText(),
                 popup_glow=self.cb_glow.isChecked(), popup_glow_style=self.cb_glow_style.currentData(),
                 popup_glow_colors=list(self.glow_colors), popup_glow_speed=self.sp_glow_speed.value(),
                 popup_glow_width=self.sp_glow_width.value(),
                 memory_enabled=self.cb_mem.isChecked(), memory_prompt_chars=self.sp_mem.value(),
                 auto_compact=self.cb_compact.isChecked(), compact_at_percent=self.sp_cat.value(),
                 compact_keep_percent=self.sp_ckeep.value(), python_timeout=self.sp_pyto.value(),
                 **{k: get() for k, get in self.sp_fields.items()})
        return self.cfg


# ---------- Hauptfenster ----------

class GlowOverlay(QWidget):
    """Leuchtrahmen über dem Popup: Licht läuft im Uhrzeigersinn um den runden Rahmen."""
    FPS = 40

    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self._angle = 90.0
        self._last = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // self.FPS)
        self._timer.timeout.connect(self._tick)

    def settings(self):
        return self.shell.win.cfg["settings"]

    def sync(self):
        """Timer je nach Sichtbarkeit und Einstellung starten/stoppen."""
        on = bool(self.settings().get("popup_glow", True)) and self.window().isVisible()
        if on and not self._timer.isActive():
            self._last = time.monotonic()
            self._timer.start()
        elif not on and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        now = time.monotonic()
        dt, self._last = min(now - self._last, 0.2), now
        period = max(0.3, float(self.settings().get("popup_glow_speed", 4.0)))
        # QConicalGradient zählt gegen den Uhrzeigersinn → Winkel verringern = Uhrzeigersinn
        self._angle = (self._angle - 360.0 * dt / period) % 360.0
        self.update(self._ring_region())

    def _ring_region(self):
        r = self.shell.frame.geometry()
        pad = 10 + int(self.settings().get("popup_glow_width", 2))
        inner = r.adjusted(pad + self.shell.RADIUS // 2, pad + self.shell.RADIUS // 2,
                           -pad - self.shell.RADIUS // 2, -pad - self.shell.RADIUS // 2)
        return QRegion(r.adjusted(-pad, -pad, pad, pad)) - QRegion(inner)

    def _gradient(self, center):
        s = self.settings()
        cols = [QColor(c) for c in (s.get("popup_glow_colors") or ["#8b7cff"]) if QColor(c).isValid()] \
            or [QColor("#8b7cff")]
        g = QConicalGradient(center, self._angle)
        if s.get("popup_glow_style", "lauflicht") == "verlauf":
            n = len(cols)
            for i, c in enumerate(cols):
                g.setColorAt(i / n, c)
            g.setColorAt(1.0, cols[0])
        else:
            # Kopf bei 0 (vorne, im Uhrzeigersinn), Schweif dahinter (gegen den Uhrzeigersinn) blendet aus
            tail = 0.42
            n = len(cols)
            for i, c in enumerate(cols):
                t = tail * i / max(1, n)
                c = QColor(c)
                c.setAlphaF(1.0 - 0.75 * (t / tail))
                g.setColorAt(t, c)
            end = QColor(cols[-1])
            end.setAlpha(0)
            g.setColorAt(tail, end)
            g.setColorAt(0.999, end)
            g.setColorAt(1.0, cols[0])
        return g

    def paintEvent(self, e):
        s = self.settings()
        if not s.get("popup_glow", True):
            return
        r = QRectF(self.shell.frame.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
        R = self.shell.RADIUS
        path = QPainterPath()
        path.addRoundedRect(r, R, R)
        width = float(s.get("popup_glow_width", 2))
        grad = self._gradient(r.center())
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(Qt.NoBrush)
        if s.get("popup_glow_style", "lauflicht") != "verlauf":
            base = QColor((s.get("popup_glow_colors") or ["#8b7cff"])[0])
            base.setAlpha(45)
            p.setPen(QPen(base, 1.0))
            p.drawPath(path)
        for extra, opacity in ((10, 0.10), (6, 0.18), (3, 0.35), (0, 1.0)):   # weiches Leuchten nach außen
            p.setOpacity(opacity)
            p.setPen(QPen(grad, width + extra, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.end()


class PopupShell(QWidget):
    """Durchsichtiger Rand mit selbst gemaltem weichen Schatten + runder Inhalt."""
    PAD, RADIUS = 16, 18

    def __init__(self, content, win):
        super().__init__()
        self.win = win
        frame = QFrame()
        frame.setObjectName("PopupFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(1, 8, 1, 1)   # Abstand oben, damit nichts über die runden Ecken malt
        fl.addWidget(content)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(self.PAD, self.PAD - 4, self.PAD, self.PAD + 4)
        lay.addWidget(frame)
        self.frame = frame
        self.glow = GlowOverlay(self)
        self.glow.raise_()

    def resizeEvent(self, e):
        self.glow.setGeometry(self.rect())
        self.glow.raise_()
        super().resizeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.glow.sync)

    def hideEvent(self, e):
        self.glow.sync()
        super().hideEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        dark = self.win.colors.get("name") == "dark"
        r = self.frame.geometry().translated(0, 4)
        for i in range(self.PAD, 0, -1):    # weicher Schatten aus vielen halbtransparenten Ringen
            alpha = int((10 if dark else 5) * (1 - i / self.PAD) ** 1.6 * 2.2)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(r.adjusted(-i, -i, i, i), self.RADIUS + i, self.RADIUS + i)
        p.end()


def _icon_btn(text, tip, slot):
    b = QPushButton(text)
    b.setObjectName("Icon")
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(slot)
    return b


_INDEX_TIMER = None
_SCHED_TIMER = None
_MCP_STARTED = False


class MainWindow(QMainWindow):
    def __init__(self, popup=False, ctrl=None):
        super().__init__()
        self.popup, self.ctrl = popup, ctrl
        self._hidden_at = 0.0
        self._was_active = False
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
        self.bridge.shell_update.connect(self.redraw)
        self.bridge.shell_done.connect(self._shell_done)
        self.bridge.screenshot_done.connect(self._screenshot_done)
        self.bridge.open_chat.connect(self._open_chat_from_notification)
        self.bridge.task_done.connect(self._task_done)
        self.bridge.stt_done.connect(self._stt_done)
        self.bridge.speech_state.connect(lambda t: self._info(t))
        self.recorder = speech.Recorder()
        self.speaker = speech.Speaker(self.cfg["settings"], on_state=self.bridge.speech_state.emit)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(250)
        self._rec_timer.timeout.connect(self._rec_tick)
        self.vision = {}             # endpoint-name -> versteht Bilder?
        self.attachments = []        # Anhänge für die nächste Nachricht
        self.setAcceptDrops(True)
        self._shell_running = False

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
        self.cb_profile.currentTextChanged.connect(lambda *_: self._estimate())

        # --- Chat + Eingabe ---
        self.view = ChatView()
        self.view.on_confirm = self._confirm_choice
        self.view.on_speak = self.speak
        self.input = InputBox()
        self.input.setPlaceholderText("Nachricht schreiben …")
        self.input.setToolTip("Enter = senden · Shift+Enter = neue Zeile · Esc = Stopp\n"
                              "/ = Befehle · !befehl = im Terminal ausführen")
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
        self.composer = composer
        cl = QHBoxLayout(composer)
        cl.setContentsMargins(10, 6, 6, 6)
        cl.addWidget(self.btn_compact, 0, Qt.AlignBottom)
        self.btn_pin = _icon_btn("📌", "Anheften: Popup bleibt offen, auch wenn du woanders klickst", self.toggle_pin)
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(bool(self.cfg["settings"].get("popup_pinned")))
        self.btn_pin.setVisible(popup)
        cl.addWidget(self.btn_pin, 0, Qt.AlignBottom)
        self.btn_attach = _icon_btn("📎", "Datei oder Bild anhängen (auch per Drag & Drop oder Strg+V)\n"
                                    "/screenshot macht ein Bildschirmfoto", self.pick_files)
        self.btn_attach.setFixedSize(36, 36)
        cl.addWidget(self.btn_attach, 0, Qt.AlignBottom)
        self.input.attach_hook = self._attach_mime
        # alle Knöpfe in der Eingabeleiste gleich hoch wie das einzeilige Eingabefeld,
        # sonst wird die Leiste höher und der Senden-Knopf sitzt nicht mittig
        for b in (self.btn_compact, self.btn_pin):
            b.setFixedSize(36, 36)
        cl.addWidget(self.input, 1)
        self.btn_mic = _icon_btn("🎤", "Sprechen (Strg+M): klicken, reden, nochmal klicken", self.toggle_record)
        self.btn_mic.setFixedSize(36, 36)
        cl.addWidget(self.btn_mic, 0, Qt.AlignBottom)
        cl.addWidget(self.btn_stop, 0, Qt.AlignBottom)
        cl.addWidget(self.btn_send, 0, Qt.AlignBottom)
        # Vorschlagsliste für /Befehle (über dem Eingabefeld)
        self.cmd_list = QListWidget()
        self.cmd_list.setObjectName("CmdList")
        self.cmd_list.setMaximumWidth(ChatView.MAX_W)
        self.cmd_list.setFocusPolicy(Qt.NoFocus)
        self.cmd_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cmd_list.setTextElideMode(Qt.ElideRight)
        self.cmd_list.itemClicked.connect(lambda it: self._cmd_complete(it, run=True))
        self.cmd_list.hide()
        self.input.key_hook = self._cmd_key
        self.input.textChanged.connect(self._cmd_suggest)
        # Infozeile unter dem Eingabefeld (Kontext, Tempo, Modell) – vor allem fürs Popup
        self.meta = QWidget()
        self.meta.setObjectName("Meta")
        self.meta.setMaximumWidth(ChatView.MAX_W)
        ml_ = QHBoxLayout(self.meta)
        ml_.setContentsMargins(14, 0, 14, 0)
        ml_.setSpacing(8)
        self.meta_bar = QProgressBar()
        self.meta_bar.setObjectName("MetaBar")
        self.meta_bar.setTextVisible(False)
        self.meta_bar.setFixedSize(70, 5)
        self.meta_ctx = QLabel("Kontext –")
        self.meta_speed = QLabel("")
        self.meta_model = QLabel("")
        for lb in (self.meta_ctx, self.meta_speed, self.meta_model):
            lb.setObjectName("MetaText")
        self.meta_model.setTextInteractionFlags(Qt.NoTextInteraction)
        ml_.addWidget(self.meta_bar, 0, Qt.AlignVCenter)
        ml_.addWidget(self.meta_ctx)
        ml_.addWidget(self.meta_speed)
        ml_.addStretch(1)
        ml_.addWidget(self.meta_model)
        self._last_tps = 0.0
        self._ctx_used = 0
        crow = QVBoxLayout()
        crow.setContentsMargins(22, 6, 22, 8)
        crow.setSpacing(5)
        self.att_bar = QWidget()
        self.att_bar.setMaximumWidth(ChatView.MAX_W)
        self.att_row = QHBoxLayout(self.att_bar)
        self.att_row.setContentsMargins(8, 0, 8, 0)
        self.att_row.setSpacing(6)
        self.att_bar.hide()
        for w_ in (self.cmd_list, self.att_bar, composer, self.meta):
            h = QHBoxLayout()
            h.addStretch(1)
            h.addWidget(w_, 100)
            h.addStretch(1)
            crow.addLayout(h)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(self.topbar)
        ml.addWidget(self.view, 1)
        ml.addLayout(crow)

        root = QWidget()
        root.setObjectName("Root")
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        rl.addWidget(self.side)
        rl.addWidget(main, 1)
        if popup:
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setCentralWidget(PopupShell(root, self))
        else:
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

        for keys, slot in (("Ctrl+N", self.new_chat), ("Ctrl+K", self.toggle_compact), ("Ctrl+M", self.toggle_record),
                           ("Ctrl+B", self.toggle_side), ("Escape", self.escape)):
            act = QAction(self)
            act.setShortcut(QKeySequence(keys))
            act.triggered.connect(slot)
            self.addAction(act)

        QApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())
        # Laufwerks-Index: beim Start und stündlich prüfen (nur ein Timer pro Programm)
        global _INDEX_TIMER
        if _INDEX_TIMER is None:
            _INDEX_TIMER = QTimer(QApplication.instance())
            _INDEX_TIMER.setInterval(3600 * 1000)
            check = lambda: self.cfg["settings"].get("drive_index", True) and T.refresh_drive_index()
            _INDEX_TIMER.timeout.connect(check)
            _INDEX_TIMER.start()
            QTimer.singleShot(20_000, check)     # nicht direkt beim Anmelden
        # Erinnerungen/Aufgaben: alle 15 s prüfen (nur ein Timer; ausgeführt vom ersten Fenster)
        global _MCP_STARTED
        if not _MCP_STARTED:
            _MCP_STARTED = True
            mcp.start_all(self.cfg["settings"].get("mcp_servers"))
            import atexit
            atexit.register(mcp.stop_all)
        global _SCHED_TIMER
        if _SCHED_TIMER is None:
            _SCHED_TIMER = QTimer(QApplication.instance())
            _SCHED_TIMER.setInterval(15_000)
            _SCHED_TIMER.timeout.connect(self._run_due_tasks)
            _SCHED_TIMER.start()
            QTimer.singleShot(5_000, self._run_due_tasks)
            self._sched_running = []
        self.apply_theme()
        self._fill_combos()
        self._load_chat_list()
        if self.chat_list.count() == 0:
            self.new_chat()
            self.refresh_models()     # sonst fragt beim allerersten Start niemand den Server
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
        shadow(self.composer, self.colors, blur=28, dy=6)
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
        self.meta.setVisible(compact)
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
        if self.cmd_list.isVisible():
            self.cmd_list.hide()
        elif self._shell_running:
            self.stop()
        elif self.agent:
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
        st = self.cfg["settings"]
        for cb, items, last in ((self.cb_ep, self.cfg["endpoints"], st.get("last_endpoint")),
                                (self.cb_profile, self.cfg["profiles"], st.get("last_profile"))):
            cur = cb.currentText() or last or ""
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
                self.vision[ep["name"]] = has_vision(ep)
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
        st = self.cfg["settings"]
        want = (st.get("last_model") if st.get("last_endpoint") == name else "") or \
            (self.endpoint() or {}).get("model") or ""
        self.cb_model.blockSignals(True)
        self.cb_model.clear()
        for mid in models:   # lange Pfade kurz anzeigen, volle ID als Daten
            self.cb_model.addItem(mid.rsplit("/", 1)[-1], mid)
        idx = self.cb_model.findData(want)
        if idx < 0 and want:      # auch kurzen Namen akzeptieren (z. B. von Hand eingetragen)
            idx = self.cb_model.findText(want.rsplit("/", 1)[-1])
        if idx >= 0:
            self.cb_model.setCurrentIndex(idx)
        elif models:              # gespeichertes Modell gibt es auf diesem Server nicht → Modell des Servers nehmen
            self.cb_model.setCurrentIndex(0)
        elif want:
            self.cb_model.setEditText(want)
        self.cb_model.blockSignals(False)
        self._sync_chat_settings()
        self._estimate()
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
        self._remember_selection()

    # Server/Modell/Profil gelten für beide Fenster (großes Fenster und Popup) und bleiben gespeichert
    def _remember_selection(self):
        if getattr(self, "_applying", False):
            return
        ep, model, prof = self.cb_ep.currentText(), self.model_id(), self.cb_profile.currentText()
        st = self.cfg["settings"]
        if not model and st.get("last_endpoint") == ep:
            model = st.get("last_model", "")          # Modelle noch nicht geladen → altes Modell behalten
        new = {"last_endpoint": ep, "last_model": model, "last_profile": prof}
        if all(st.get(k) == v for k, v in new.items()):
            return
        st.update(new)
        C.save_config(self.cfg)
        if self.ctrl:
            for w in (self.ctrl.main, self.ctrl.popup):
                if w is not None and w is not self:
                    w.apply_selection(ep, model, prof)

    def apply_selection(self, ep, model, prof):
        """Auswahl aus dem anderen Fenster übernehmen."""
        self._applying = True
        try:
            self.cfg["settings"].update(last_endpoint=ep, last_model=model, last_profile=prof)
            for cb, val in ((self.cb_profile, prof), (self.cb_ep, ep)):
                if val and cb.findText(val) >= 0:
                    cb.blockSignals(True)
                    cb.setCurrentText(val)
                    cb.blockSignals(False)
            idx = self.cb_model.findData(model)
            if idx >= 0 and self.n_ctx.get(ep) is not None:
                self.cb_model.blockSignals(True)
                self.cb_model.setCurrentIndex(idx)
                self.cb_model.blockSignals(False)
                self._sync_chat_settings()
            else:
                self.refresh_models()               # anderer Server → Modelle holen, last_model wird gewählt
            self._update_meta()
            self._estimate()
        finally:
            self._applying = False

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
        self._estimate()
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
        # Server/Modell/Profil bleiben wie gewählt (gelten für alle Chats und beide Fenster)
        self.ctx_bar.setValue(0)
        self.refresh_models()
        self.redraw()
        self._estimate()

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
    # --- Sprache ---
    def toggle_record(self):
        if self.recorder.active:
            path = self.recorder.stop()
            self._rec_timer.stop()
            self.btn_mic.setText("🎤")
            self.btn_mic.setStyleSheet("")
            self.input.setPlaceholderText("Nachricht schreiben …")
            if not path:
                self._info("Aufnahme zu kurz")
                return
            self._info("✍ Wandle Sprache in Text um … (beim ersten Mal lädt das Modell)")
            settings = dict(self.cfg["settings"])

            def job():
                try:
                    text, info = speech.transcribe(path, settings)
                    self.bridge.stt_done.emit(text, info, "")
                except Exception as e:
                    self.bridge.stt_done.emit("", "", str(e))
                finally:
                    path.unlink(missing_ok=True)
            threading.Thread(target=job, daemon=True).start()
            return
        try:
            self.speaker.stop()
            self.recorder.start()
        except Exception as e:
            self._info(f"⚠ Mikrofon: {e}")
            return
        self.btn_mic.setText("⏺")
        self.btn_mic.setStyleSheet("color: #e5484d;")
        self._rec_timer.start()
        self._rec_tick()

    def _rec_tick(self):
        self.input.setPlaceholderText(f"🎤 Aufnahme {self.recorder.seconds():.0f} s … nochmal klicken oder Strg+M")

    def _stt_done(self, text, info, err):
        if err:
            self._info(f"⚠ Spracherkennung: {err}")
            return
        if not text:
            self._info("Nichts verstanden")
            return
        cur = self.input.toPlainText()
        self.input.setPlainText((cur + " " if cur.strip() else "") + text)
        c = self.input.textCursor()
        c.movePosition(c.MoveOperation.End)
        self.input.setTextCursor(c)
        self._info(f"✍ erkannt ({info})")
        if self.cfg["settings"].get("stt_autosend"):
            self.send()
        else:
            self.input.setFocus()

    def speak(self, text):
        self.speaker.settings = self.cfg["settings"]
        self.speaker.speak(text)

    # --- Erinnerungen / geplante Aufgaben ---
    def _run_due_tasks(self):
        for t in S.due(time.time()):
            late = " (verpasst – spike lief nicht)" if t.get("late") else ""
            if t["kind"] == "erinnerung":
                S.notify(f"⏰ Erinnerung{late}", t["text"], urgent=True)
            else:
                self._run_task(t, late)

    def _run_task(self, t, late=""):
        ep = self.endpoint()
        if not ep:
            S.notify("🤖 Geplante Aufgabe", f"Kein Server eingerichtet: {t['text']}")
            return
        prof = next((p for p in self.cfg["profiles"] if p["name"] == (t.get("profile") or "Alles")), self.profile())
        chat = C.new_chat(ep["name"], self.model_id(), prof["name"])
        chat["title"] = f"🤖 {t['text']}"[:45]
        chat["messages"].append({"role": "user", "content": f"(Geplante Aufgabe{late}, {time.strftime('%d.%m.%Y %H:%M')}) "
                                                            f"{t['text']}"})
        n_ctx = self.n_ctx.get(ep["name"]) or self.cfg["settings"]["default_n_ctx"]
        a = Agent(chat, ep, self.model_id(), prof, self.cfg["settings"], ToolBox(self.cfg["settings"], self.browser),
                  n_ctx)
        a.auto_deny = True
        self._sched_running.append(a)

        def done(a=a, chat=chat):      # läuft im Hintergrund-Thread → nur speichern und Signal schicken
            C.save_chat(chat)
            if a in self._sched_running:
                self._sched_running.remove(a)
            last = next((m.get("content") for m in reversed(chat["messages"])
                         if m["role"] == "assistant" and m.get("content")), "(keine Antwort)")
            self.bridge.task_done.emit(chat["id"], f"🤖 {t['text'][:60]}{late}", last)
        a.finished.connect(done, Qt.DirectConnection)
        a.start()

    def _task_done(self, chat_id, title, text):
        self._load_chat_list_keep()
        S.notify(title, text, on_open=lambda: self.bridge.open_chat.emit(chat_id))

    def _load_chat_list_keep(self):
        """Chatliste auffrischen, ohne den offenen Chat zu wechseln."""
        if self.agent:
            return
        cur = self.chat["id"] if self.chat else None
        self.chat_list.blockSignals(True)
        self._load_chat_list()
        for i in range(self.chat_list.count()):
            if self.chat_list.item(i).data(Qt.UserRole) == cur:
                self.chat_list.setCurrentRow(i)
        self.chat_list.blockSignals(False)

    def _open_chat_from_notification(self, chat_id):
        if self.ctrl:
            self.ctrl.show_main(chat_id)
        else:
            self.select_chat(chat_id)
            self.show()
            self.raise_()

    # --- Anhänge ---
    def _user_message(self, text):
        if not self.attachments:
            return {"role": "user", "content": text}
        atts, self.attachments = self.attachments, []
        self._show_attachments()
        limit = int(self.cfg["settings"].get("attach_max_chars", 30000))
        parts = [text] if text else []
        for a in atts:
            if a["kind"] in ("text", "pdf"):
                parts.append(A.file_text(a, limit))
        msg = {"role": "user", "content": "\n\n".join(parts) or "(siehe Bild)", "_text": text,
               "_attachments": atts}
        imgs = [a["path"] for a in atts if a["kind"] == "image"]
        if imgs:
            msg["_images"] = imgs
        return msg

    def add_attachment(self, att):
        if len(self.attachments) >= 10:
            self._info("Höchstens 10 Anhänge pro Nachricht")
            return
        self.attachments.append(att)
        self._show_attachments()
        if att["kind"] == "image" and not self.vision.get(self.cb_ep.currentText()):
            self._info("⚠ Das aktuelle Modell kann keine Bilder sehen – es bekommt nur einen Hinweis aufs Bild")
        self.input.setFocus()

    def _show_attachments(self):
        while self.att_row.count():
            it = self.att_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, a in enumerate(self.attachments):
            icon = {"image": "🖼", "pdf": "📕"}.get(a["kind"], "📄")
            b = QPushButton(f"{icon} {a['name'][:28]}  ✕")
            b.setObjectName("CopyBtn")
            b.setToolTip(f"{a.get('orig') or a['path']}\n{A.human_size(a['size'])} – klicken zum Entfernen")
            b.clicked.connect(lambda _=False, i=i: (self.attachments.pop(i), self._show_attachments()))
            self.att_row.addWidget(b)
        self.att_row.addStretch(1)
        self.att_bar.setVisible(bool(self.attachments))

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Anhängen", str(self.toolbox.cwd))
        for f in files:
            self._attach_path(f)

    def _attach_path(self, path):
        try:
            self.add_attachment(A.prepare(path))
        except Exception as e:
            self._info(f"⚠ {e}")

    def _attach_mime(self, mime):
        if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
            for u in mime.urls():
                if u.isLocalFile():
                    self._attach_path(u.toLocalFile())
            return True
        if mime.hasImage():
            img = mime.imageData()
            if img is not None and not img.isNull():
                self.add_attachment(A.from_image(img, "Zwischenablage.png"))
                return True
        return False

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if self._attach_mime(e.mimeData()):
            e.acceptProposedAction()

    def take_screenshot(self, question="", active_window=False):
        """Popup kurz ausblenden, Bildschirmfoto machen, als Anhang übernehmen (und ggf. gleich fragen)."""
        self._shot_question = question
        self._shot_reshow = self.popup and self.isVisible()
        if self._shot_reshow:
            self.hide()

        def job():
            time.sleep(0.45 if self._shot_reshow else 0.1)
            try:
                self.bridge.screenshot_done.emit(A.screenshot(active_window), "")
            except Exception as e:
                self.bridge.screenshot_done.emit(None, str(e))
        threading.Thread(target=job, daemon=True).start()

    def _screenshot_done(self, att, err):
        if self._shot_reshow and self.ctrl and not self.isVisible():
            self.ctrl.toggle_popup()
        if err:
            self._info(f"⚠ Screenshot: {err}")
            return
        self.add_attachment(att)
        if self._shot_question:
            self.input.setPlainText(self._shot_question)
            self.send()

    def _new_agent(self):
        ep = self.endpoint()
        n_ctx = self.n_ctx.get(ep["name"]) or self.cfg["settings"]["default_n_ctx"]
        a = Agent(self.chat, ep, self.model_id(), self.profile(), self.cfg["settings"], self.toolbox, n_ctx)
        a.usage.connect(self._usage)
        a.vision = self.toolbox.vision = bool(self.vision.get(ep["name"]))
        return a

    def _estimate(self):
        """Kontext-Belegung des aktuellen Chats schätzen (ohne Anfrage an den Server)."""
        if self.agent or self.chat is None or not self.endpoint():
            return
        try:
            self._new_agent().estimate()
        except Exception as e:
            print("Schätzung fehlgeschlagen:", e)

    def send(self):
        text = self.input.toPlainText().strip()
        if self.agent and self.agent.pending:
            if text:      # Text während einer Freigabe = „Nein, mach stattdessen …“
                self.input.clear()
                self.agent.answer(False, reason=text)
            return
        if text.startswith("/") and self.run_command(text):
            return
        if text.startswith("!") and len(text) > 1:
            self.run_user_shell(text[1:].strip())
            return
        if self._shell_running:
            self._info("Warte, bis der Terminal-Befehl fertig ist (Stopp bricht ab)")
            return
        if (not text and not self.attachments) or self.agent or self.chat is None:
            return
        ep = self.endpoint()
        if not ep:
            QMessageBox.warning(self, "Kein Server", "Bitte zuerst unter ⚙ Einstellungen einen Endpunkt anlegen.")
            return
        self._sync_chat_settings()
        self.input.clear()
        self.chat["messages"].append(self._user_message(text))
        if self.chat["title"] == "Neuer Chat":
            self.chat["title"] = text.replace("\n", " ")[:45]
            it = self.chat_list.currentItem()
            if it:
                it.setText(self.chat["title"])
        self.agent = self._new_agent()
        self.agent.speed.connect(self._speed)
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
        if self._shell_running:
            self.toolbox.kill()
        if self.agent:
            self.agent.stop()
            self.status.setText("Abbruch …")

    def _info(self, text):
        self.status.setText(text)
        if self.is_compact():
            self.input.setPlaceholderText(text or "Nachricht schreiben …")

    def _confirm(self, text):
        """Freigabe erscheint als Karte im Chat (ChatView) – hier nur nach vorn holen und Hinweis zeigen."""
        self.redraw(force_bottom=True)
        self._info("🔐 Wartet auf deine Freigabe")
        if self.isVisible() and not self.isActiveWindow():
            self.raise_()
            self.activateWindow()

    def _confirm_choice(self, choice):
        if self.agent and self.agent.pending:
            self.agent.answer(choice != "no", remember=(choice == "always"))
            self._info("")
            self.input.setFocus()

    # --- !befehl: selbst im Terminal ausführen ---
    def run_user_shell(self, cmd):
        if self.agent or self._shell_running or self.chat is None:
            self._info("Bitte warten, bis die Antwort fertig ist")
            return
        self.input.clear()
        sh = {"cmd": cmd, "cwd": str(self.toolbox.cwd), "out": "", "running": True}
        msg = {"role": "user", "content": "", "_shell": sh}
        self.chat["messages"].append(msg)
        if self.chat["title"] == "Neuer Chat":
            self.chat["title"] = ("$ " + cmd)[:45]
            it = self.chat_list.currentItem()
            if it:
                it.setText(self.chat["title"])
        self._shell_running = True
        self.btn_send.hide()
        self.btn_stop.show()
        self.redraw(force_bottom=True)
        last = [0.0]

        def on_out(text):
            sh["out"] = (sh["out"] + text)[-50000:]
            if time.monotonic() - last[0] > 0.05:
                last[0] = time.monotonic()
                self.bridge.shell_update.emit()

        def job():
            try:
                rc, out = self.toolbox.shell(cmd, on_output=on_out)
            except Exception as e:
                rc, out = -1, f"Fehler: {e}"
            sh.update(out=out, exit=rc, running=False)
            limit = int(self.cfg["settings"].get("tool_output_max_chars", 6000))
            msg["content"] = (f"[Ich habe selbst im Terminal ausgeführt]\n{sh['cwd']}$ {cmd}\n"
                              f"{T.clip_tail(out, limit)}\nexit={rc}")
            self.bridge.shell_done.emit()
        threading.Thread(target=job, daemon=True).start()

    def _shell_done(self):
        self._shell_running = False
        self.btn_stop.hide()
        self.btn_send.show()
        C.save_chat(self.chat)
        self.redraw()
        self._estimate()
        self.input.setFocus()

    def _stats(self, text, used, n_ctx):
        if n_ctx:
            self.ctx_bar.setValue(min(100, int(used * 100 / n_ctx)))
            self.ctx_bar.setToolTip(f"{used} / {n_ctx} Tokens")
        self.status.setText(text)

    def _finished(self):
        if self.cfg["settings"].get("tts_auto") and self.chat and self.chat["messages"]:
            last = self.chat["messages"][-1]
            if last["role"] == "assistant" and last.get("content"):
                self.speak(last["content"])
        C.save_chat(self.chat)
        self.agent = None
        self.btn_stop.hide()
        self.btn_send.show()
        self.input.setPlaceholderText("Nachricht schreiben …")
        self.redraw()
        self._update_meta()
        self.input.setFocus()

    # --- Infozeile: Kontext und Tempo ---
    def _usage(self, used, n_ctx, tps):
        self._ctx_used, self._ctx_max = used, n_ctx
        if tps > 0:
            self._last_tps = tps
        if n_ctx:
            self.ctx_bar.setValue(min(100, int(used * 100 / n_ctx)))
            self.ctx_bar.setToolTip(f"{used:,} / {n_ctx:,} Tokens".replace(",", "."))
        self._update_meta()

    def _speed(self, tps):
        self.meta_speed.setText(f"⚡ {tps:.1f} tok/s")

    def _update_meta(self):
        fmt = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)
        used, n = self._ctx_used, getattr(self, "_ctx_max", 0)
        pct = min(100, int(used * 100 / n)) if n else 0
        self.meta_bar.setValue(pct)
        lim = int(self.cfg["settings"].get("compact_at_percent", 70)) * 0.75   # Anteil am Gesamtkontext, grob
        col = self.colors.get("danger", "#e5484d") if pct >= 90 else ("#e5a100" if pct >= lim else self.colors["accent"])
        self.meta_bar.setStyleSheet(f"QProgressBar#MetaBar {{ background: {self.colors['border']}; border: none;"
                                    f"border-radius: 2px; }} QProgressBar#MetaBar::chunk {{ background: {col};"
                                    f"border-radius: 2px; }}")
        summ = " · 📦 zusammengefasst" if self.chat and self.chat.get("summary") else ""
        self.meta_ctx.setText(f"Kontext {pct} % · {fmt(used)} / {fmt(n)}{summ}" if n else "Kontext –")
        tip = f"Belegt: {used:,} von {n:,} Tokens".replace(",", ".")
        if summ:
            tip += f"\nÄlterer Verlauf ist zusammengefasst (bis Nachricht {self.chat.get('summary_upto')})."
        tip += "\n/compact fasst den Verlauf jetzt zusammen."
        self.meta_ctx.setToolTip(tip)
        if not self.agent:
            self.meta_speed.setText(f"⚡ {self._last_tps:.1f} tok/s" if self._last_tps else "")
        model = (self.model_id() or "").rsplit("/", 1)[-1].removesuffix(".gguf")
        if len(model) > 26:
            model = model[:25] + "…"
        think = {"an": "💭 an", "aus": "💭 aus"}.get(self.profile().get("thinking", "standard"), "")
        self.meta_model.setText(" · ".join(x for x in (model, self.cb_profile.currentText(), think) if x))
        self.meta_model.setToolTip(f"Server: {self.cb_ep.currentText()}\nModell: {self.model_id()}\n"
                                   f"Profil: {self.cb_profile.currentText()}\n(/modell, /profil, /server zum Wechseln)")

    # --- /Befehle ---
    COMMANDS = [
        # (name, aliasse, argument, beschreibung)
        ("/new", ("/neu", "/clear"), "", "Neuer Chat"),
        ("/compact", ("/compress", "/komprimieren"), "[Hinweis]", "Verlauf jetzt zusammenfassen – macht Kontext frei"),
        ("/kontext", ("/context",), "", "Kontext-Belegung anzeigen"),
        ("/profil", ("/profile",), "<Name>", "Profil wechseln"),
        ("/modell", ("/model",), "<Name>", "Modell wechseln"),
        ("/server", ("/endpoint",), "<Name>", "Server wechseln"),
        ("/screenshot", ("/bildschirm",), "[Frage]", "Bildschirmfoto anhängen (mit Frage: gleich senden)"),
        ("/aufgaben", ("/tasks", "/erinnerungen"), "", "Geplante Erinnerungen und Aufgaben anzeigen"),
        ("/vorlesen", ("/speak", "/sag"), "[Text]", "Letzte Antwort (oder Text) vorlesen"),
        ("/stimme", ("/voice",), "<Name>", "Piper-Stimme wechseln"),
        ("/merk", ("/remember",), "<Text>", "Etwas dauerhaft ins Gedächtnis schreiben"),
        ("/denken", ("/think",), "[an|aus|standard]", "Nachdenken des Modells für dieses Profil schalten"),
        ("/stop", (), "", "Laufende Antwort abbrechen"),
        ("/gross", ("/big", "/fenster"), "", "Chat im großen Fenster öffnen"),
        ("/einstellungen", ("/settings",), "", "Einstellungen öffnen"),
        ("/index", ("/reindex",), "", "Laufwerks-Index für die Dateisuche jetzt erneuern"),
        ("/quit", ("/beenden", "/exit"), "", "spike beenden"),
        ("/hilfe", ("/help",), "", "Alle Befehle zeigen"),
    ]

    def _cmd_lookup(self, word):
        word = word.lower()
        return next((c for c in self.COMMANDS if word == c[0] or word in c[1]), None)

    def _cmd_choices(self, cmd):
        if cmd == "/profil":
            return [p["name"] for p in self.cfg["profiles"]]
        if cmd == "/server":
            return [e["name"] for e in self.cfg["endpoints"]]
        if cmd == "/stimme":
            return [n for n, _ in speech.piper_voices()]
        if cmd == "/denken":
            return ["an", "aus", "standard"]
        if cmd == "/modell":
            return [self.cb_model.itemText(i) for i in range(self.cb_model.count())]
        return []

    def _cmd_suggest(self):
        text = self.input.toPlainText()
        self.cmd_list.clear()
        if not text.startswith("/") or "\n" in text or self.agent:
            self.cmd_list.hide()
            return
        word, _, arg = text.partition(" ")
        if _:    # Befehl fertig getippt → Argumente vorschlagen (Profile, Modelle, Server)
            c = self._cmd_lookup(word)
            for ch in (self._cmd_choices(c[0]) if c else []):
                if arg.strip().lower() in ch.lower():
                    it = QListWidgetItem(f"{c[0]} {ch}")
                    it.setData(Qt.UserRole, f"{c[0]} {ch}")
                    self.cmd_list.addItem(it)
        else:
            w = word.lower()
            for name, aliases, argh, desc in self.COMMANDS:
                if any(n.startswith(w) for n in (name,) + aliases):
                    it = QListWidgetItem(f"{name} {argh}".strip() + f"   –  {desc}")
                    it.setData(Qt.UserRole, name)
                    it.setData(Qt.UserRole + 1, bool(argh) and not argh.startswith("["))
                    self.cmd_list.addItem(it)
        if self.cmd_list.count():
            self.cmd_list.setCurrentRow(0)
            row_h = self.cmd_list.sizeHintForRow(0) or 26
            self.cmd_list.setFixedHeight(min(8, self.cmd_list.count()) * row_h + 10)
            self.cmd_list.show()
        else:
            self.cmd_list.hide()

    def _cmd_complete(self, it, run=False):
        value = it.data(Qt.UserRole)
        needs_arg = bool(it.data(Qt.UserRole + 1))
        if needs_arg or not run:
            self.input.setPlainText(value + (" " if needs_arg or " " not in value else ""))
            cur = self.input.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            self.input.setTextCursor(cur)
            self.input.setFocus()
            return
        self.input.setPlainText(value)
        self.send()

    def _cmd_key(self, key):
        if not self.cmd_list.isVisible():
            return False
        row = self.cmd_list.currentRow()
        if key in (Qt.Key_Down, Qt.Key_Up):
            n = self.cmd_list.count()
            self.cmd_list.setCurrentRow((row + (1 if key == Qt.Key_Down else -1)) % n)
            return True
        if key == Qt.Key_Tab:
            it = self.cmd_list.currentItem()
            if it:
                self._cmd_complete(it)
            return True
        if key == Qt.Key_Escape:
            self.cmd_list.hide()
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            it = self.cmd_list.currentItem()
            typed = self.input.toPlainText().strip()
            # Nur übernehmen, wenn das Getippte noch unvollständig ist – sonst normal ausführen
            if it and typed != it.data(Qt.UserRole) and not self._cmd_ready(typed):
                self._cmd_complete(it, run=True)
                return True
        return False

    def _cmd_ready(self, typed):
        word, _, arg = typed.partition(" ")
        c = self._cmd_lookup(word)
        if not c:
            return False
        if c[2] and not c[2].startswith("[") and not arg.strip():
            return False
        if c[0] in ("/profil", "/server", "/modell") and arg.strip():
            return arg.strip() in self._cmd_choices(c[0])
        return True

    def run_command(self, text) -> bool:
        """Führt /befehl aus. Gibt False zurück, wenn es kein Befehl ist (dann normal senden)."""
        word, _, arg = text.strip().partition(" ")
        arg = arg.strip()
        c = self._cmd_lookup(word)
        if not c:
            if re.fullmatch(r"/[\w-]+", word):
                self._info(f"Unbekannter Befehl {word} – /hilfe zeigt alle")
                return True
            return False      # z. B. ein Pfad wie /etc/fstab → als Nachricht senden
        name = c[0]
        self.cmd_list.hide()
        if name not in ("/stop", "/quit") and self.agent:
            self._info("Bitte warten, bis die Antwort fertig ist (oder /stop)")
            return True
        self.input.clear()
        if name == "/new":
            self.new_chat()
        elif name == "/compact":
            self.compact_now(arg)
        elif name == "/kontext":
            self._estimate()
            n = getattr(self, "_ctx_max", 0)
            pct = int(self._ctx_used * 100 / n) if n else 0
            summ = (f" · zusammengefasst bis Nachricht {self.chat['summary_upto']}"
                    if self.chat and self.chat.get("summary") else "")
            self._info(f"Kontext: {self._ctx_used:,} / {n:,} Tokens ({pct} %){summ}".replace(",", "."))
        elif name in ("/profil", "/server", "/modell"):
            cb = {"/profil": self.cb_profile, "/server": self.cb_ep, "/modell": self.cb_model}[name]
            items = [cb.itemText(i) for i in range(cb.count())]
            hit = next((t for t in items if t.lower() == arg.lower()), None) or \
                next((t for t in items if arg and arg.lower() in t.lower()), None)
            if not hit:
                self._info(f"Nicht gefunden: „{arg}“. Möglich: {', '.join(items[:8])}")
                return True
            cb.setCurrentIndex(items.index(hit))
            self._sync_chat_settings()
            self._estimate()
            self._info(f"{ {'/profil': 'Profil', '/server': 'Server', '/modell': 'Modell'}[name] }: {hit}")
        elif name == "/stimme":
            voices = speech.piper_voices()
            hit = next((p for n, p in voices if n.lower() == arg.lower()), None) or \
                next((p for n, p in voices if arg and arg.lower() in n.lower()), None)
            if not hit:
                self._info(f"Piper-Stimme „{arg}“ nicht gefunden. Installiert: " +
                           (", ".join(n for n, _ in voices) or "keine") + " – mehr unter ⚙ → Sprache")
                return True
            self.cfg["settings"]["piper_model"] = hit
            C.save_config(self.cfg)
            self._info(f"🔊 Piper-Stimme: {Path(hit).stem}")
            self.speaker.settings = self.cfg["settings"]
            self.speaker.speak("So klinge ich jetzt.", engine="piper")
        elif name == "/vorlesen":
            last = next((m.get("content") for m in reversed(self.chat["messages"] if self.chat else [])
                         if m["role"] == "assistant" and m.get("content")), "")
            if arg or last:
                self.speak(arg or last)
            else:
                self._info("Noch keine Antwort zum Vorlesen")
        elif name == "/aufgaben":
            tasks = sorted(S.load(), key=lambda t: t["next"])
            if not tasks:
                self._info("Nichts geplant. Sag z. B. „erinnere mich in 20 min an den Tee“.")
            else:
                QMessageBox.information(self, "Geplant", "\n".join(S.describe(t) for t in tasks) +
                                        "\n\nLöschen: „lösche Aufgabe <id>“ schreiben.")
        elif name == "/screenshot":
            self.take_screenshot(arg)
        elif name == "/denken":
            prof = self.profile()
            cur = prof.get("thinking", "standard")
            new = {"an": "an", "on": "an", "ein": "an", "aus": "aus", "off": "aus",
                   "standard": "standard"}.get(arg.lower(), "aus" if cur != "aus" else "an")
            prof["thinking"] = new
            C.save_config(self.cfg)
            self._update_meta()
            self._info(f"💭 Denken im Profil „{prof.get('name')}“: {new}")
        elif name == "/merk":
            if not arg:
                self._info("Was soll ich mir merken? /merk <Text>")
                return True
            m = memory.add(arg)
            self._info(f"🧠 Gemerkt: {m['text'][:60]}")
        elif name == "/stop":
            self.stop()
            self.speaker.stop()
        elif name == "/gross":
            if self.popup and self.ctrl:
                self.ctrl.show_main(self.chat["id"] if self.chat else None)
            elif self.is_compact():
                self.toggle_compact()
        elif name == "/einstellungen":
            self.open_settings()
        elif name == "/index":
            age = T.drive_index_age()
            msg = T.refresh_drive_index(max_age_h=0)
            self._info(msg + (f" (bisher {age:.0f} h alt)" if age is not None else "") +
                       " – dauert einige Minuten, spike bleibt benutzbar.")
        elif name == "/quit":
            if self.ctrl:
                self.ctrl.quit()
            else:
                QApplication.quit()
        elif name == "/hilfe":
            self.input.setPlainText("/")
            cur = self.input.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            self.input.setTextCursor(cur)
        return True

    def compact_now(self, hint=""):
        if self.agent or self.chat is None or not self.endpoint():
            return
        if not self.chat["messages"]:
            self._info("Der Chat ist noch leer.")
            return
        self.agent = self._new_agent()
        self.agent.changed.connect(self.redraw)
        self.agent.error.connect(lambda e: QMessageBox.warning(self, "Fehler", e))
        self.agent.info.connect(self._info)
        self.agent.finished.connect(self._compact_done)
        self.btn_send.hide()
        self.btn_stop.show()
        self.agent.start_compact(hint)

    def _compact_done(self):
        self._finished()
        self._estimate()

    def redraw(self, force_bottom=False):
        msgs = self.chat["messages"] if self.chat else []
        live = self.agent.live if self.agent else None
        if force_bottom:
            self.view.scroll_bottom()
        self.view.render(msgs, live, self.colors, self.is_compact(),
                         int((self.chat or {}).get("summary_upto") or 0), (self.chat or {}).get("summary", ""),
                         self.agent.pending if self.agent else None, self.agent.live_tool if self.agent else None)

    # --- Einstellungen ---
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            new = dlg.result_config()
            mcp_changed = new["settings"].get("mcp_servers") != self.cfg["settings"].get("mcp_servers")
            self.cfg["endpoints"], self.cfg["profiles"] = new["endpoints"], new["profiles"]
            self.cfg["settings"].update(new["settings"])   # gleiches dict, ToolBox sieht Änderungen
            C.save_config(self.cfg)
            if mcp_changed:
                mcp.start_all(self.cfg["settings"].get("mcp_servers"))
                self._info("MCP-Server werden neu gestartet …")
            self.config_changed()
            if self.ctrl:     # anderes Fenster (Popup ↔ großes Fenster) mitziehen
                for w in (self.ctrl.main, self.ctrl.popup):
                    if w is not None and w is not self:
                        fresh = C.load_config()
                        w.cfg["endpoints"], w.cfg["profiles"] = fresh["endpoints"], fresh["profiles"]
                        w.cfg["settings"].update(fresh["settings"])
                        w.config_changed()

    def config_changed(self):
        self._fill_combos()
        self.apply_theme()
        self.refresh_models()
        glow = getattr(self.centralWidget(), "glow", None)
        if glow:
            glow.sync()

    def changeEvent(self, e):
        # Popup schließt sich, sobald man daneben klickt (wie das Lautstärke-Popup)
        if self.popup and e.type() == QEvent.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active:      # nur schließen, wenn es vorher wirklich aktiv war
                QTimer.singleShot(120, self._maybe_autohide)
        super().changeEvent(e)

    def toggle_pin(self):
        pinned = self.btn_pin.isChecked()
        self.cfg["settings"]["popup_pinned"] = pinned
        C.save_config(self.cfg)
        self.btn_pin.setToolTip("Angeheftet – Klick löst das Popup wieder (schließt dann beim Klick daneben)"
                                if pinned else "Anheften: Popup bleibt offen, auch wenn du woanders klickst")
        self.input.setFocus()

    def _maybe_autohide(self):
        if self.btn_pin.isChecked():
            return
        if self.isVisible() and not self.isActiveWindow() and QApplication.activeModalWidget() is None \
                and QApplication.activeWindow() is None:
            self.hide()

    def showEvent(self, e):
        self._was_active = False
        super().showEvent(e)

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
