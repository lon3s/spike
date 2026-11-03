"""Reiter „Handy“ in den Einstellungen: Webserver an/aus, QR-Code zum Verbinden, Schlüssel erneuern."""
import shutil
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from . import web as W


def qr_pixmap(text: str, scale=6) -> QPixmap | None:
    if not shutil.which("qrencode"):
        return None
    try:
        png = subprocess.run(["qrencode", "-t", "PNG", "-s", str(scale), "-m", "2", "-o", "-", text],
                             capture_output=True, timeout=5, check=True).stdout
    except Exception:
        return None
    pm = QPixmap()
    pm.loadFromData(png, "PNG")
    return pm


class PhoneTab(QWidget):
    def __init__(self, settings: dict, webapp):
        super().__init__()
        self.web = webapp
        f = QFormLayout(self)
        f.addRow(QLabel("<b>spike auf dem Handy</b> – der Chat läuft im Handy-Browser, Modell und Werkzeuge "
                        "bleiben hier auf dem PC."))
        self.cb_on = QCheckBox("Handy-Oberfläche einschalten (Webserver im Heimnetz)")
        self.cb_on.setChecked(bool(settings.get("web_enabled")))
        f.addRow(self.cb_on)
        self.sp_port = QSpinBox()
        self.sp_port.setRange(1024, 65535)
        self.sp_port.setValue(int(settings.get("web_port") or W.DEFAULT_PORT))
        self.sp_port.setMaximumWidth(120)
        f.addRow("Port", self.sp_port)
        self.cb_https = QCheckBox("HTTPS (eigenes Zertifikat) – nötig fürs Mikrofon im Handy-Browser")
        self.cb_https.setChecked(bool(settings.get("web_https", True)))
        f.addRow(self.cb_https)

        self.status = QLabel()
        self.status.setWordWrap(True)
        f.addRow(self.status)
        row = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        row.addWidget(self.qr, 0, Qt.AlignTop)
        side = QVBoxLayout()
        self.target = QComboBox()
        self.target.setToolTip("Für unterwegs: Tailscale-Adresse (erscheint, sobald Tailscale läuft)")
        self.target.currentIndexChanged.connect(lambda _i: self._show_link())
        side.addWidget(self.target)
        self.link = QLineEdit()
        self.link.setReadOnly(True)
        side.addWidget(QLabel("Link (enthält den Schlüssel – nicht weitergeben):"))
        side.addWidget(self.link)
        btns = QHBoxLayout()
        b_copy = QPushButton("Link kopieren")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.link.text()))
        b_new = QPushButton("Neuen Schlüssel erzeugen")
        b_new.setToolTip("Danach gelten alte Links nicht mehr – jedes Handy muss neu verbunden werden.")
        b_new.clicked.connect(self._new_token)
        btns.addWidget(b_copy)
        btns.addWidget(b_new)
        btns.addStretch(1)
        side.addLayout(btns)
        self.token = QLabel()
        self.token.setTextInteractionFlags(Qt.TextSelectableByMouse)
        side.addWidget(self.token)
        side.addStretch(1)
        row.addLayout(side, 1)
        self.box = QWidget()
        self.box.setLayout(row)
        f.addRow(self.box)

        tips = QLabel(
            "<b>So geht's:</b> QR-Code mit der Handy-Kamera scannen → beim ersten Mal warnt der Browser vor dem "
            "Zertifikat („Erweitert → Weiter“) → im Browser-Menü <i>Zum Startbildschirm hinzufügen</i>.<br>"
            "<b>Unterwegs:</b> nur über ein VPN wie Tailscale (Anleitung unten) – den Port nie ins Internet "
            "freigeben, spike kann Befehle auf diesem PC ausführen.")
        tips.setWordWrap(True)
        f.addRow(tips)
        self.b_ts = QPushButton("▸  Unterwegs mit Tailscale – Anleitung")
        self.b_ts.setFlat(True)
        self.b_ts.setStyleSheet("text-align: left; font-weight: 600; padding: 4px 0;")
        self.b_ts.clicked.connect(self._toggle_ts)
        f.addRow(self.b_ts)
        self.ts_help = QLabel()
        self.ts_help.setWordWrap(True)
        self.ts_help.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.ts_help.setOpenExternalLinks(True)
        self.ts_help.hide()
        f.addRow(self.ts_help)
        self.fw = QLabel()
        self.fw.setWordWrap(True)
        self.fw.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(self.fw)
        self.refresh()

    def values(self) -> dict:
        return {"web_enabled": self.cb_on.isChecked(), "web_port": self.sp_port.value(),
                "web_https": self.cb_https.isChecked()}

    def _toggle_ts(self):
        show = not self.ts_help.isVisible()
        self.ts_help.setVisible(show)
        self.b_ts.setText(("▾" if show else "▸") + "  Unterwegs mit Tailscale – Anleitung")

    def _ts_text(self, port):
        have = shutil.which("tailscale")
        install = ("Tailscale ist installiert ✓" if have else
                   "Arch/CachyOS: <code>sudo pacman -S tailscale</code> · andere: "
                   "<code>curl -fsSL https://tailscale.com/install.sh | sh</code>")
        fw = (f"<li>Firewall (ufw): <code>sudo ufw allow in on tailscale0 to any port {port} proto tcp</code></li>"
              if shutil.which("ufw") else "")
        return (
            "Tailscale verbindet deine Geräte über ein privates, verschlüsseltes Netz – das Handy erreicht spike "
            "dann auch im Mobilfunk, ohne dass am Router etwas freigegeben wird. Für private Nutzung kostenlos."
            "<ol>"
            f"<li><b>PC:</b> {install}<br>"
            "<code>sudo systemctl enable --now tailscaled</code><br>"
            "<code>sudo tailscale up</code> – den angezeigten Link öffnen und anmelden (Google, GitHub, …)</li>"
            "<li><b>Handy:</b> App <i>Tailscale</i> aus dem Play Store bzw. App Store installieren, mit "
            "<b>demselben Konto</b> anmelden, VPN einschalten.</li>"
            f"{fw}"
            "<li>Diesen Reiter neu öffnen: oben im Auswahlfeld erscheint <b>„Unterwegs – Tailscale“</b>. "
            "Den QR-Code damit scannen und auf dem Handy wie gewohnt zum Startbildschirm hinzufügen. Zu Hause und "
            "unterwegs funktioniert dann dieser eine Link, solange die Tailscale-App an ist.</li>"
            "</ol>"
            "<b>Optional – ohne Zertifikatswarnung:</b> In der Tailscale-Verwaltung "
            "(<a href='https://login.tailscale.com/admin/dns'>login.tailscale.com → DNS</a>) „MagicDNS“ und "
            "„HTTPS Certificates“ einschalten, dann am PC:<br>"
            f"<code>sudo tailscale serve --bg https+insecure://localhost:{port}</code><br>"
            "spike ist dann unter <code>https://&lt;pc-name&gt;.&lt;dein-netz&gt;.ts.net</code> mit gültigem "
            "Zertifikat erreichbar (Adresse zeigt <code>tailscale serve status</code>). Beim ersten Öffnen den "
            "Schlüssel von oben eintippen.")

    def _show_link(self):
        if not (self.web and self.web.httpd):
            return
        link = self.web.link(self.target.currentData() or "")
        self.link.setText(link)
        pm = qr_pixmap(link)
        if pm:
            self.qr.setPixmap(pm)
        else:
            self.qr.setText("QR-Code braucht\ndas Paket qrencode")

    def _new_token(self):
        W.new_token()
        self.refresh()

    def refresh(self):
        web = self.web
        running = bool(web and web.httpd)
        tok = W.load_state()["token"]
        self.token.setText(f"Schlüssel zum Eintippen: <code>{tok}</code>")
        if not web:
            self.status.setText("Die Handy-Oberfläche gibt es nur, wenn spike mit Tray läuft.")
            self.box.hide()
            return
        if running:
            web.refresh_ips()          # z. B. Tailscale neu → Zertifikat muss die Adresse kennen
            self.status.setText(f"● Läuft unter <b>{web.url}</b>" + (f"<br>⚠ {web.error}" if web.error else ""))
            cur = self.target.currentData()
            self.target.blockSignals(True)
            self.target.clear()
            for ip in W.lan_ips():
                if ip.startswith("127."):
                    continue
                self.target.addItem(f"Unterwegs – Tailscale ({ip})" if W.is_tailscale(ip) else f"Heimnetz ({ip})", ip)
            i = self.target.findData(cur)
            self.target.setCurrentIndex(max(0, i))
            self.target.blockSignals(False)
            self.target.setVisible(self.target.count() > 1)
            self._show_link()
            self.box.show()
        else:
            self.status.setText(("⚠ " + web.error) if web.error else
                                "Aus. Einschalten und <b>Speichern</b> – dann erscheint hier der QR-Code.")
            self.box.hide()
        port = self.sp_port.value()
        self.ts_help.setText(self._ts_text(port))
        ip = W.lan_ips()[0]
        net = ".".join(ip.split(".")[:3]) + ".0/24"
        if shutil.which("ufw"):
            self.fw.setText(f"Firewall: Falls das Handy nicht verbindet, Port im Heimnetz freigeben:<br>"
                            f"<code>sudo ufw allow from {net} to any port {port} proto tcp</code>")
        elif shutil.which("firewall-cmd"):
            self.fw.setText(f"Firewall: <code>sudo firewall-cmd --permanent --add-port={port}/tcp && "
                            f"sudo firewall-cmd --reload</code>")
