"""Tray-Symbol, Popup-Chat unten rechts, Einzel-Instanz.

spike            → großes Fenster (bzw. holt das laufende spike nach vorn)
spike --tray     → nur Tray-Symbol starten (für Autostart)
spike --popup    → Popup ein/aus (zum Binden an eine Tastenkombination)
spike --text     → markierten Text im Popup bearbeiten (Übersetzen, Korrigieren …; Tastenkürzel)
spike --quit     → laufendes spike beenden
"""
import ctypes
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QMargins, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import plugins
from .tools import Browser, load_plugins

ICON = Path(__file__).resolve().parent / "assets" / "spike.svg"
SOCKET = f"spike-{os.getuid()}"
POPUP_SIZE = (500, 720)   # inkl. Schattenrand


def _layer_shell(widget, margin=0) -> bool:
    """Dockt ein Fenster unter KDE/Wayland per layer-shell unten rechts an (wie System-Popups)."""
    if QApplication.platformName() != "wayland":
        return False
    try:
        import shiboken6
        lib = ctypes.CDLL("libLayerShellQtInterface.so.6")
        get = lib._ZN12LayerShellQt6Window3getEP7QWindow
        get.restype, get.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        widget.winId()
        win = widget.windowHandle()
        get(shiboken6.getCppPointer(win)[0])
        ls = next(c for c in win.children() if c.metaObject().className() == "LayerShellQt::Window")
        ls.setProperty("anchors", 2 | 8)              # unten | rechts
        ls.setProperty("layer", 2)                    # über normalen Fenstern
        ls.setProperty("keyboardInteractivity", 2)    # Tastatur bei Bedarf
        ls.setProperty("margins", QMargins(0, 0, margin, margin))
        ls.setProperty("scope", "spike-popup")
        return True
    except Exception as e:
        print("layer-shell nicht verfügbar:", e)
        return False


class _WebBridge(QObject):
    saved = Signal(str, object, bool)       # chat_id, geänderte Felder (oder None), gelöscht
    job_done = Signal(dict)                 # Hintergrundauftrag beendet (aus dem Beobachter-Thread)
    job_answered = Signal(str, str, str)    # chat_id, titel, antwort – Modell hat ohne Fenster reagiert


class Controller:
    def __init__(self, app):
        from .ui import MainWindow
        self.app, self._MainWindow = app, MainWindow
        self.browser = Browser()
        load_plugins()
        from . import config as _C, speech as _speech
        _speech.warm_up(_C.load_config()["settings"])   # faster-whisper-Suche (~1 s) vorab im Hintergrund
        self.main = None
        self.popup = None
        self.layer = False

        self.tray = QSystemTrayIcon(QIcon(str(ICON)), app)
        self.tray.setToolTip("spike – Klick: Chat · Rechtsklick: Menü")
        menu = QMenu()
        menu.addAction("Chat-Popup", self.toggle_popup)
        menu.addAction("Großes Fenster", lambda: self.show_main())
        menu.addAction("Neuer Chat", self.new_chat_popup)
        menu.addAction("Markierten Text bearbeiten", self.text_action)
        menu.addAction("Handy verbinden …", self.show_phone)
        menu.addSeparator()
        menu.addAction("Hilfe", self.show_help)
        menu.addAction("Beenden", self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        app.aboutToQuit.connect(self.browser.shutdown)

        # Handy-Oberfläche (Webserver im Heimnetz, nur wenn eingeschaltet)
        from . import config as C
        from .web import WebApp
        self._wb = _WebBridge()
        self._wb.saved.connect(self._web_saved)
        self.web = WebApp(self.browser, self)
        self.web.apply(C.load_config()["settings"])
        app.aboutToQuit.connect(self.web.stop)
        # Hintergrundaufträge: laufende aus der letzten Sitzung weiter beobachten, Fertig-Meldungen verteilen
        from . import jobs as _J
        self.job_runs: dict[str, object] = {}      # chat_id -> Agent ohne Fenster (Chat war nirgends offen)
        self._wb.job_done.connect(self._job_done)
        self._wb.job_answered.connect(self._job_answered)
        _J.ON_DONE.append(lambda job: self._wb.job_done.emit(dict(job)))
        from .ui import job_bus
        job_bus().changed.connect(self._jobs_tooltip)
        _J.restore()
        # Popup schon kurz nach dem Start unsichtbar vorbereiten – sonst dauert der erste Klick ~1,5 s
        QTimer.singleShot(2500, self._ensure_popup)

    # --- Handy: Abgleich mit den Fenstern (Aufrufe kommen aus Hintergrund-Threads) ---
    def busy(self, chat_id):
        return chat_id in self.job_runs or any(
            w is not None and w.agent and w.chat and w.chat.get("id") == chat_id for w in (self.main, self.popup))

    # --- Hintergrundauftrag fertig → Nachricht in den Chat, aus dem er gestartet wurde ---
    @staticmethod
    def job_message(job: dict) -> dict:
        from . import jobs as J
        rc = job.get("rc")
        kopf = (f"⚙ Hintergrundauftrag [{job['id']}] „{job['name']}“ "
                + ("fertig (exit 0)" if rc == 0 else "beendet – Rückgabewert unbekannt (spike lief nicht durchgehend)"
                   if rc is None else f"abgebrochen (Signal {-rc})" if rc < 0 else f"FEHLGESCHLAGEN (exit {rc})")
                + f" nach {J._dur(job)}.")
        tail = J.tail(job["id"], lines=25)
        text = kopf + f"\nLog: {job['log']} (mehr mit job_status)\n--- Log-Ende ---\n{tail[-3000:]}"
        return {"role": "user", "content": text, "_job": job["id"]}

    def _jobs_tooltip(self):
        from . import jobs as J
        n = len(J.running())
        self.tray.setToolTip("spike – Klick: Chat · Rechtsklick: Menü"
                             + (f"\n⚙ {n} Hintergrundauftrag läuft" if n == 1 else
                                f"\n⚙ {n} Hintergrundaufträge laufen" if n else ""))

    def _job_done(self, job: dict, versuch=0):
        from . import config as C, scheduler as S
        cid = job.get("chat") or ""
        msg = self.job_message(job)
        if cid:
            for w in (self.main, self.popup):      # Chat in einem Fenster offen → dort einschieben
                if w is not None and w.chat and w.chat.get("id") == cid and w.inject_message(msg):
                    return
            if self.web and cid in self.web.runs or cid in self.job_runs:
                if versuch < 120:                  # läuft gerade woanders – gleich nochmal
                    QTimer.singleShot(5000, lambda: self._job_done(job, versuch + 1))
                return
            try:
                chat = C.load_chat(cid)
            except FileNotFoundError:
                chat = None
            if chat is not None and self._run_headless(chat, msg):
                return
        title = f"⚙ {job['name'][:50]}"
        S.notify(title, msg["content"].split("\n", 1)[0], on_open=(lambda: self.show_main(cid)) if cid else None,
                 urgent=job.get("rc") not in (0, None))

    def _run_headless(self, chat: dict, msg: dict) -> bool:
        """Chat ist nirgends offen: Modell ohne Fenster reagieren lassen (wie geplante Aufgaben, ohne Freigaben)."""
        from . import config as C, scheduler as S
        from .agent import Agent
        from .tools import ToolBox
        win = self.main or self.popup
        if win is None:
            return False
        ep = win.endpoint()
        if not ep:
            return False
        cfg = win.cfg
        prof = next((p for p in cfg["profiles"] if p["name"] == chat.get("profile")), None) or win.profile()
        n_ctx = win.n_ctx.get(ep["name"]) or cfg["settings"]["default_n_ctx"]
        tb = ToolBox(cfg["settings"], self.browser)
        cwd = Path(chat.get("cwd") or Path.home())
        tb.cwd = cwd if cwd.is_dir() else Path.home()
        chat["messages"].append(msg)
        a = Agent(chat, ep, win.model_id(), prof, cfg["settings"], tb, n_ctx)
        a.vision = tb.vision = bool(win.vision.get(ep["name"]))
        a.auto_deny = True
        cid = chat["id"]
        self.job_runs[cid] = a

        def done():
            C.save_chat(chat)
            self.job_runs.pop(cid, None)
            last = next((m.get("content") for m in reversed(chat["messages"])
                         if m["role"] == "assistant" and m.get("content")), "(keine Antwort)")
            self._wb.job_answered.emit(cid, msg["content"].split("\n", 1)[0], last)
        a.finished.connect(done, Qt.DirectConnection)
        C.save_chat(chat)
        a.start()
        return True

    def _job_answered(self, cid, title, text):
        from . import scheduler as S
        self.saved(cid)              # Fenster/Chatliste auffrischen
        S.notify(title, text[:400], on_open=lambda: self.show_main(cid))

    def saved(self, chat_id, meta=None, deleted=False):
        self._wb.saved.emit(chat_id, meta, deleted)

    def _web_saved(self, chat_id, meta, deleted):
        from . import config as C
        for w in (self.main, self.popup):
            if w is None:
                continue
            open_here = w.chat is not None and w.chat.get("id") == chat_id
            if open_here and not w.agent:
                if deleted:
                    w.chat = None
                    w.new_chat()
                elif meta:
                    w._apply_meta(chat_id, **meta)
                else:
                    try:
                        w.chat = C.load_chat(chat_id)
                        w.redraw(force_bottom=True)
                    except FileNotFoundError:
                        pass
            w._load_chat_list_keep()

    def show_phone(self):
        self.show_main()
        self.main.open_settings(tab="Handy")

    def tray_active(self):
        return self.tray.isVisible()

    def _tray_clicked(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_popup()
        elif reason == QSystemTrayIcon.MiddleClick:
            self.show_main()

    # --- Popup ---
    def _ensure_popup(self):
        if self.popup:
            return
        p = self._MainWindow(popup=True, ctrl=self)
        p.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        p.resize(*POPUP_SIZE)
        p.setObjectName("Popup")
        self.layer = _layer_shell(p)
        self.popup = p

    def _place_popup(self):
        if self.layer:
            return
        geo = (self.app.screenAt(self.tray.geometry().center()) or self.app.primaryScreen()).availableGeometry()
        self.popup.move(geo.right() - POPUP_SIZE[0], geo.bottom() - POPUP_SIZE[1])

    def toggle_popup(self):
        self._ensure_popup()
        p = self.popup
        if p.isVisible():
            p.hide()
            return
        if time.monotonic() - p._hidden_at < 0.35:   # Klick aufs Tray hat das Popup gerade geschlossen
            return
        self._follow_main()
        self._place_popup()
        p.show()
        p.raise_()
        p.activateWindow()
        p.redraw(force_bottom=True)
        p.input.setFocus()

    def _follow_main(self):
        """Wurde zuletzt im großen Fenster gearbeitet (offen oder schon geschlossen), macht das Popup mit
        demselben Chat weiter – wie umgekehrt beim Öffnen des Fensters aus dem Popup."""
        from . import config as C
        m, p = self.main, self.popup
        if m is None or m.chat is None or p.agent:
            return
        if not m.isVisible() and m._used_at <= p._used_at:
            return                          # zuletzt war das Popup dran → seinen Chat behalten
        if p.chat is not None and p.chat.get("id") == m.chat.get("id"):
            return
        if not m.chat["messages"]:          # leerer neuer Chat im Fenster → im Popup auch frisch anfangen
            if p.chat is None or p.chat["messages"]:
                p.chat = None
                p.new_chat()
            return
        C.save_chat(m.chat)                 # neuesten Stand sichern, damit das Popup ihn findet und lädt
        p.select_chat(m.chat["id"])

    def new_chat_popup(self):
        self._ensure_popup()
        if not self.popup.isVisible():
            self.toggle_popup()
        self.popup.new_chat()

    # --- großes Fenster ---
    def show_main(self, chat_id=None):
        if not self.main:
            self.main = self._MainWindow(ctrl=self)
        if self.popup and self.popup.isVisible():
            self.popup.hide()
        if chat_id:
            self.main.select_chat(chat_id)
        self.main.show()
        self.main.raise_()
        self.main.activateWindow()

    def show_help(self):
        self.show_main()
        self.main.show_help()

    def quit(self):
        for w in (self.main, self.popup):
            if w and w.agent:
                w.agent.stop()
            if w and w.chat and w.chat["messages"]:
                from . import config as C
                C.save_chat(w.chat)
        self.tray.hide()
        self.app.quit()

    def command(self, cmd):
        {"popup": self.toggle_popup, "main": self.show_main, "quit": self.quit, "text": self.text_action,
         "tray": lambda: None}.get(cmd, self.show_main)()

    def text_action(self):
        """Markierten Text (sonst Zwischenablage) holen und im Popup die Schnellaktionen zeigen."""
        import subprocess
        text = ""
        for args in (["wl-paste", "--primary", "--no-newline"], ["wl-paste", "--no-newline"]):
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    text = r.stdout
                    break
            except Exception:
                pass
        if not text:
            text = QApplication.clipboard().text(QApplication.clipboard().Mode.Selection) or \
                QApplication.clipboard().text()
        self._ensure_popup()
        if not self.popup.isVisible():
            self.toggle_popup()
        self.popup.show_selection(text)


def main():
    args = sys.argv[1:]
    cmd = "main"
    for a, c in (("--tray", "tray"), ("--popup", "popup"), ("--quit", "quit"), ("--text", "text")):
        if a in args:
            cmd = c

    app = QApplication(sys.argv)
    app.setApplicationName("spike")
    app.setDesktopFileName("spike")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(ICON)))
    app.setQuitOnLastWindowClosed(False)

    # Läuft spike schon? Dann nur den Befehl weitergeben.
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if sock.waitForConnected(300):
        sock.write(cmd.encode())
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return
    if cmd == "quit":
        return

    QLocalServer.removeServer(SOCKET)
    server = QLocalServer()
    server.listen(SOCKET)
    ctrl = Controller(app)

    def on_conn():
        c = server.nextPendingConnection()
        c.waitForReadyRead(500)
        ctrl.command(bytes(c.readAll()).decode().strip())
        c.disconnectFromServer()
    server.newConnection.connect(on_conn)

    if not ctrl.tray_active() and cmd == "tray":
        cmd = "main"     # kein Tray vorhanden → normales Fenster
    ctrl.command(cmd)
    sys.exit(app.exec())
