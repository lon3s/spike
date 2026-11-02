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

from PySide6.QtCore import QMargins, Qt
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


class Controller:
    def __init__(self, app):
        from .ui import MainWindow
        self.app, self._MainWindow = app, MainWindow
        self.browser = Browser()
        load_plugins()
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
        menu.addSeparator()
        menu.addAction("Beenden", self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        app.aboutToQuit.connect(self.browser.shutdown)

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
        self._place_popup()
        p.show()
        p.raise_()
        p.activateWindow()
        p.redraw(force_bottom=True)
        p.input.setFocus()

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
