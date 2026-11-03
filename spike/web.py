"""Handy-Oberfläche: kleiner Webserver im Heimnetz (nur Python-Standardbibliothek).

Das Handy zeigt nur den Chat – Modell, Werkzeuge, Dateien und Freigaben laufen weiter hier auf dem PC.

- Aus/an, Port und HTTPS unter ⚙ Einstellungen → Handy (settings web_enabled / web_port / web_https).
- Zugang nur mit Schlüssel (Token). Er steckt im Link bzw. QR-Code und liegt in ~/.config/spike/web/web.json.
- HTTPS mit selbst erzeugtem Zertifikat (openssl), damit das Handy-Mikrofon im Browser funktioniert.
  Beim ersten Öffnen warnt der Browser einmal – „Erweitert → Weiter“.
- Live-Updates per Server-Sent Events (/api/events).
"""
import copy
import hmac
import json
import mimetypes
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import Qt

from . import attachments as A
from . import config as C
from . import scheduler as S
from . import speech
from .agent import Agent
from .client import get_n_ctx, has_vision, list_models
from .tools import ToolBox, short_args

STATIC = Path(__file__).resolve().parent / "web"
ICON_SVG = Path(__file__).resolve().parent / "assets" / "spike.svg"
WEB_DIR = C.CONFIG_DIR / "web"
STATE_FILE = WEB_DIR / "web.json"
DEFAULT_PORT = 8765


# ---------- gespeicherter Zustand (eigene Datei, damit sich PC-Fenster und Handy nicht überschreiben) ----------

_state_lock = threading.Lock()


def load_state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        d = {}
    if not d.get("token"):
        d["token"] = secrets.token_urlsafe(18)
        save_state(d)
    return d


def save_state(d: dict) -> None:
    with _state_lock:
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        tmp.chmod(0o600)
        tmp.replace(STATE_FILE)


def new_token() -> str:
    d = load_state()
    d["token"] = secrets.token_urlsafe(18)
    save_state(d)
    return d["token"]


def lan_ips() -> list[str]:
    """IPv4-Adressen im Heimnetz (ohne Docker/Loopback). Die mit Standard-Route zuerst."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))          # kein Paket wird gesendet, nur Route bestimmen
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and not parts[1].startswith(("lo", "docker", "br-", "veth", "virbr")):
                ip = parts[3].split("/")[0]
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def ensure_cert() -> tuple[Path, Path] | None:
    """Selbst signiertes Zertifikat (10 Jahre) für alle aktuellen IPs. Neu, wenn eine IP fehlt."""
    crt, key = WEB_DIR / "cert.pem", WEB_DIR / "key.pem"
    ips = lan_ips()
    st = load_state()
    if crt.exists() and key.exists() and set(ips) <= set(st.get("cert_ips") or []):
        return crt, key
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    san = ",".join([f"IP:{ip}" for ip in ips] + ["IP:127.0.0.1", f"DNS:{socket.gethostname()}", "DNS:localhost"])
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-keyout", str(key), "-out", str(crt), "-subj", "/CN=spike",
                        "-addext", f"subjectAltName={san}"], capture_output=True, check=True, timeout=60)
        key.chmod(0o600)
    except Exception as e:
        print("Handy: Zertifikat konnte nicht erstellt werden:", e)
        return None
    st = load_state()
    st["cert_ips"] = ips
    save_state(st)
    return crt, key


# ---------- Nachrichten → einfache Blöcke für den Browser ----------

def _tail(s, n):
    s = s or ""
    return s if len(s) <= n else "…" + s[-n:]


def _att_info(a):
    return {"name": a.get("name", ""), "kind": a.get("kind", ""),
            "url": f"/api/file?p={urllib.parse.quote(a.get('path', ''))}" if a.get("kind") == "image" else ""}


def blocks(chat) -> list[dict]:
    msgs = chat.get("messages", [])
    results = {m.get("tool_call_id"): m for m in msgs if m["role"] == "tool"}
    upto = int(chat.get("summary_upto") or 0)
    out = []
    for i, m in enumerate(msgs):
        if upto and i == upto:
            out.append({"t": "sum"})
        r = m["role"]
        if r == "user":
            if m.get("_shell"):
                sh = m["_shell"]
                out.append({"t": "shell", "cmd": sh.get("cmd", ""), "out": _tail(sh.get("out", ""), 4000),
                            "exit": sh.get("exit"), "running": bool(sh.get("running"))})
            elif m.get("_auto"):
                out.append({"t": "note", "text": "Bild vom Werkzeug",
                            "atts": [_att_info(a) for a in m.get("_attachments", [])]})
            else:
                text = m.get("_text", m.get("content") if isinstance(m.get("content"), str) else "")
                out.append({"t": "user", "text": text or "", "atts": [_att_info(a) for a in m.get("_attachments", [])]})
        elif r == "assistant":
            if m.get("content") or m.get("_reasoning"):
                out.append({"t": "ai", "text": m.get("content") or "", "think": _tail(m.get("_reasoning", ""), 6000)})
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                res = results.get(tc.get("id"))
                content = (res or {}).get("content") or ""
                out.append({"t": "tool", "id": tc.get("id"), "name": f.get("name", ""),
                            "args": short_args(f.get("arguments", ""), 160),
                            "result": _tail(content, 3000) if res else None,
                            "diff": (res or {}).get("_diff", ""),
                            "denied": content.startswith("Vom Nutzer abgelehnt")})
    return out


# ---------- Verbindung zur PC-Oberfläche ----------

class Hub:
    """Ein gemeinsamer Wecker für alle offenen Live-Verbindungen."""
    def __init__(self):
        self.cond = threading.Condition()
        self.ver = 0
        self.notes = deque(maxlen=30)     # {"id", "title", "body", "chat"}
        self._nid = 0

    def bump(self):
        with self.cond:
            self.ver += 1
            self.cond.notify_all()

    def note(self, title, body, chat=""):
        with self.cond:
            self._nid += 1
            self.notes.append({"id": self._nid, "title": title, "body": body[:400], "chat": chat, "time": time.time()})
            self.ver += 1
            self.cond.notify_all()


class Run:
    def __init__(self, agent, chat):
        self.agent, self.chat = agent, chat
        self.speed, self.info, self.error = 0.0, "", ""
        self.done = False


class WebApp:
    def __init__(self, browser, desktop=None):
        self.browser = browser
        self.desktop = desktop        # Controller: busy(chat_id), saved(chat_id)
        self.hub = Hub()
        self.runs: dict[str, Run] = {}
        self.uploads: dict[str, dict] = {}
        self.errors: dict[str, str] = {}      # letzter Fehler pro Chat (bleibt nach dem Ende sichtbar)
        self._models = {}             # endpoint-name -> (zeit, models, n_ctx, vision)
        self._lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.url = ""
        self.error = ""
        S.LISTENERS.append(lambda t, b: self.hub.note(t, b))

    # --- Server an/aus ---
    def apply(self, settings: dict):
        want = bool(settings.get("web_enabled"))
        port = int(settings.get("web_port") or DEFAULT_PORT)
        https = bool(settings.get("web_https", True))
        key = (want, port, https)
        if self.httpd and getattr(self, "_key", None) == key:
            return
        self.stop()
        self._key = key
        self.error = ""
        if not want:
            return
        app = self

        class H(Handler):
            web = app
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), H)
            httpd.daemon_threads = True
        except OSError as e:
            self.error = f"Port {port} ist belegt oder nicht erlaubt: {e}"
            print("Handy:", self.error)
            return
        scheme = "http"
        if https:
            pair = ensure_cert()
            if pair:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(str(pair[0]), str(pair[1]))
                httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
                scheme = "https"
            else:
                self.error = "openssl fehlt – läuft ohne HTTPS (Mikrofon im Browser geht dann nicht)"
        self.httpd = httpd
        self.url = f"{scheme}://{lan_ips()[0]}:{port}"
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        print("Handy-Oberfläche:", self.url)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.hub.bump()

    def link(self) -> str:
        return f"{self.url}/?t={load_state()['token']}" if self.url else ""

    # --- Konfiguration / Auswahl ---
    def _cfg(self):
        return C.load_config()

    def selection(self, cfg) -> dict:
        s, st = cfg["settings"], load_state()
        eps = [e["name"] for e in cfg["endpoints"]]
        ep = st.get("endpoint") if st.get("endpoint") in eps else s.get("last_endpoint")
        if ep not in eps:
            ep = eps[0] if eps else ""
        profs = [p["name"] for p in cfg["profiles"]]
        prof = st.get("profile") if st.get("profile") in profs else s.get("last_profile")
        if prof not in profs:
            prof = profs[0] if profs else ""
        model = st.get("model") if st.get("endpoint") == ep and st.get("model") else (
            s.get("last_model") if s.get("last_endpoint") == ep else "")
        return {"endpoint": ep, "model": model or "", "profile": prof, "thinking": st.get("thinking", "")}

    def models(self, ep, force=False):
        hit = self._models.get(ep["name"])
        if hit and not force and time.time() - hit[0] < 120:
            return hit[1:]
        try:
            models = list_models(ep)
            res = (models, get_n_ctx(ep) or 0, has_vision(ep), "")
        except Exception as e:
            res = ([], 0, False, str(e))
        self._models[ep["name"]] = (time.time(), *res)
        return res

    def state(self, force=False) -> dict:
        cfg = self._cfg()
        sel = self.selection(cfg)
        ep = next((e for e in cfg["endpoints"] if e["name"] == sel["endpoint"]), None)
        models, n_ctx, vision, err = self.models(ep, force) if ep else ([], 0, False, "Kein Server eingerichtet")
        if models and sel["model"] not in models:
            short = [m for m in models if m.rsplit("/", 1)[-1] == sel["model"].rsplit("/", 1)[-1]]
            sel["model"] = short[0] if short else models[0]
        prof = next((p for p in cfg["profiles"] if p["name"] == sel["profile"]), {})
        return {
            "endpoints": [{"name": e["name"], "url": e.get("url", "")} for e in cfg["endpoints"]],
            "models": models, "error": err, "n_ctx": n_ctx or cfg["settings"].get("default_n_ctx", 8192),
            "vision": vision,
            "profiles": [{"name": p["name"], "tools": p.get("tools", []), "thinking": p.get("thinking", "standard"),
                          "about": (p.get("system_prompt") or "").split(". ")[0][:90]} for p in cfg["profiles"]],
            "sel": {**sel, "thinking": sel["thinking"] or prof.get("thinking", "standard")},
            "host": socket.gethostname(), "stt": bool(speech._find_python(cfg["settings"]) or
                                                   cfg["settings"].get("stt_url")),
            "tts": speech.piper_ready(cfg["settings"]),
        }

    def select(self, data):
        st = load_state()
        for k in ("endpoint", "model", "profile", "thinking"):
            if k in data:
                st[k] = str(data[k])
        if "endpoint" in data and "model" not in data:
            st["model"] = ""
        save_state(st)

    # --- Chats ---
    def chat(self, cid):
        run = self.runs.get(cid)
        if run:
            return run.chat
        return C.load_chat(cid)

    def chat_list(self, archived=False):
        out = []
        for c in C.list_chats(archived):
            f = C.CHATS_DIR / f"{c['id']}.json"
            try:
                mt = f.stat().st_mtime
            except OSError:
                mt = 0
            out.append({**c, "mtime": mt, "running": c["id"] in self.runs})
        return out

    def live(self, cid) -> dict:
        run = self.runs.get(cid)
        if not run:
            return {"running": False, "error": self.errors.get(cid, "")}
        a = run.agent
        lt = a.live_tool
        return {"running": True, "n": len(run.chat["messages"]),
                "live": {"content": (a.live or {}).get("content", ""),
                         "think": _tail((a.live or {}).get("reasoning", ""), 3000)} if a.live else None,
                "tool": {"id": lt["id"], "out": _tail(lt.get("out", ""), 4000)} if lt else None,
                "pending": dict(a.pending) if a.pending else None,
                "speed": round(run.speed, 1), "info": run.info, "error": run.error}

    def send(self, cid, text, att_ids):
        cfg = self._cfg()
        s = cfg["settings"]
        if cid and cid in self.runs:
            raise ValueError("In diesem Chat läuft schon eine Antwort.")
        if cid and self.desktop and self.desktop.busy(cid):
            raise ValueError("Dieser Chat antwortet gerade am PC.")
        chat = C.load_chat(cid) if cid else None
        sel = self.selection(cfg)
        ep = next((e for e in cfg["endpoints"] if e["name"] == sel["endpoint"]), None)
        if not ep:
            raise ValueError("Kein Server eingerichtet (am PC unter ⚙ Einstellungen).")
        models, n_ctx, vision, err = self.models(ep)
        model = sel["model"] or (models[0] if models else ep.get("model", ""))
        prof = copy.deepcopy(next((p for p in cfg["profiles"] if p["name"] == sel["profile"]), cfg["profiles"][0]))
        if sel["thinking"]:
            prof["thinking"] = sel["thinking"]
        if chat is None:
            chat = C.new_chat(ep["name"], model, prof["name"])
        chat.update(endpoint=ep["name"], model=model, profile=prof["name"])
        atts = [self.uploads.pop(i) for i in att_ids if i in self.uploads]
        msg = {"role": "user", "content": text}
        if atts:
            limit = int(s.get("attach_max_chars", 30000))
            parts = [text] if text else []
            parts += [A.file_text(a, limit) for a in atts if a["kind"] in ("text", "pdf")]
            msg = {"role": "user", "content": "\n\n".join(parts) or "(siehe Bild)", "_text": text, "_attachments": atts}
            imgs = [a["path"] for a in atts if a["kind"] == "image"]
            if imgs:
                msg["_images"] = imgs
        chat["messages"].append(msg)
        if chat.get("title", "Neuer Chat") == "Neuer Chat":
            chat["title"] = (text or (atts[0]["name"] if atts else "Neuer Chat")).replace("\n", " ")[:45]
        C.save_chat(chat)
        self.errors.pop(chat["id"], None)

        tb = ToolBox(s, self.browser)
        agent = Agent(chat, ep, model, prof, s, tb, n_ctx or s.get("default_n_ctx", 8192))
        agent.vision = tb.vision = vision
        run = Run(agent, chat)
        hub = self.hub
        cid = chat["id"]

        def on_speed(v):
            run.speed = v
        def on_info(t):
            run.info = t
            hub.bump()
        def on_error(e):
            run.error = self.errors[cid] = e
            hub.bump()
        def on_confirm(_t):
            hub.note("🔐 spike wartet auf deine Freigabe", chat.get("title", ""), cid)
        def on_finished():
            C.save_chat(chat)
            run.done = True
            with self._lock:
                self.runs.pop(cid, None)
            hub.bump()
            if self.desktop:
                self.desktop.saved(cid)
        D = Qt.DirectConnection
        agent.changed.connect(hub.bump, D)
        agent.speed.connect(on_speed, D)
        agent.usage.connect(lambda u, n, tps: on_speed(tps) if tps > 0 else None, D)
        agent.info.connect(on_info, D)
        agent.error.connect(on_error, D)
        agent.confirm_request.connect(on_confirm, D)
        agent.finished.connect(on_finished, D)
        with self._lock:
            self.runs[cid] = run
        if self.desktop:
            self.desktop.saved(cid)       # Chatliste am PC zeigt den neuen Chat
        agent.start()
        hub.bump()
        return cid

    def upload(self, name, data: bytes) -> dict:
        name = Path(name or "datei").name[:80] or "datei"
        tmpdir = Path(tempfile.mkdtemp(prefix="spike-up-"))
        p = tmpdir / name
        p.write_bytes(data)
        try:
            att = A.prepare(p)
        finally:
            p.unlink(missing_ok=True)
            tmpdir.rmdir()
        uid = uuid.uuid4().hex[:10]
        self.uploads[uid] = att
        return {"id": uid, "name": att["name"], "kind": att["kind"], "size": A.human_size(att.get("size", len(data))),
                "url": f"/api/file?p={urllib.parse.quote(att['path'])}" if att["kind"] == "image" else ""}

    def transcribe(self, data: bytes, mime: str) -> str:
        ext = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
               "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aac": ".aac"}.get(mime.split(";")[0].strip(), ".webm")
        f = Path(tempfile.gettempdir()) / f"spike-web-{uuid.uuid4().hex[:8]}{ext}"
        f.write_bytes(data)
        try:
            text, _info = speech.transcribe(f, self._cfg()["settings"])
            return text.strip()
        finally:
            f.unlink(missing_ok=True)

    def tts(self, text: str) -> bytes:
        s = self._cfg()["settings"]
        if not speech.piper_ready(s):
            raise ValueError("Piper ist am PC nicht eingerichtet")
        f = Path(tempfile.gettempdir()) / f"spike-web-tts-{uuid.uuid4().hex[:8]}.wav"
        try:
            r = subprocess.run([speech.piper_bin(s), "-m", speech.piper_model(s), "-f", str(f)],
                               input=speech.speakable(text)[:4000], text=True, capture_output=True, timeout=180)
            if not f.exists() or f.stat().st_size < 100:
                raise RuntimeError(f"Piper: {(r.stderr or '').strip()[-200:] or 'keine Ausgabe'}")
            return f.read_bytes()
        finally:
            f.unlink(missing_ok=True)


# ---------- HTTP ----------

_icon_cache = {}


def icon_png(size: int) -> bytes:
    if size not in _icon_cache:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
        img = QImage(size, size, QImage.Format_ARGB32)
        img.fill(QColor("#14151a"))
        p = QPainter(img)
        pad = int(size * 0.16)
        QSvgRenderer(str(ICON_SVG)).render(p, QRectF(pad, pad, size - 2 * pad, size - 2 * pad))
        p.end()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        _icon_cache[size] = bytes(ba)
    return _icon_cache[size]


_fails = {}


class Handler(BaseHTTPRequestHandler):
    web: WebApp = None
    protocol_version = "HTTP/1.1"
    server_version = "spike"

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        try:      # TLS-Handshake erst hier (sonst blockiert ein hängender Client den Server)
            if isinstance(self.connection, ssl.SSLSocket):
                self.connection.settimeout(15)
                self.connection.do_handshake()
                self.connection.settimeout(None)
        except Exception:
            pass

    # --- Antworten ---
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, text):
        self._send(code, {"error": text})

    def _body(self, limit=40 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Datei ist zu groß (max. 40 MB)")
        return self.rfile.read(n) if n else b""

    def _json(self) -> dict:
        b = self._body(2 * 1024 * 1024)
        return json.loads(b or b"{}")

    # --- Zugang ---
    def _token_ok(self) -> bool:
        tok = load_state()["token"]
        got = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "spike_token":
                got = urllib.parse.unquote(v)
        got = got or (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(got) and hmac.compare_digest(got.encode(), tok.encode())

    def _cookie(self, tok):
        secure = "; Secure" if isinstance(self.connection, ssl.SSLSocket) else ""
        return f"spike_token={urllib.parse.quote(tok)}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict{secure}"

    def _login(self, tok) -> bool:
        ip = self.client_address[0]
        n, t = _fails.get(ip, (0, 0))
        if n >= 5 and time.time() - t < 60:
            time.sleep(1)
            return False
        if tok and hmac.compare_digest(tok.encode(), load_state()["token"].encode()):
            _fails.pop(ip, None)
            return True
        _fails[ip] = (n + 1, time.time())
        time.sleep(0.6)
        return False

    # --- Routen ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._err(500, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except (ValueError, FileNotFoundError) as e:
            self._err(400, str(e))
        except Exception as e:
            traceback.print_exc()
            self._err(500, f"{type(e).__name__}: {e}")

    def _get(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        if path == "/" and q.get("t"):        # Link aus dem QR-Code: Schlüssel als Cookie merken
            if self._login(q["t"]):
                return self._send(303, b"", headers={"Location": "/", "Set-Cookie": self._cookie(q["t"])})
            return self._send(303, b"", headers={"Location": "/?falsch=1"})
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path in ("/app.js", "/app.css", "/manifest.webmanifest"):
            return self._static(path[1:])
        if path.startswith("/icon-") and path.endswith(".png"):
            size = 512 if "512" in path else 192
            return self._send(200, icon_png(size), "image/png", {"Cache-Control": "max-age=86400"})
        if path == "/icon.svg":
            return self._send(200, ICON_SVG.read_bytes(), "image/svg+xml")
        if not path.startswith("/api/"):
            return self._err(404, "Nicht gefunden")
        if not self._token_ok():
            return self._err(401, "Nicht angemeldet")
        w = self.web
        if path == "/api/state":
            return self._send(200, w.state(force=q.get("neu") == "1"))
        if path == "/api/chats":
            return self._send(200, {"chats": w.chat_list(q.get("archiv") == "1"),
                                    "archived": len(C.list_chats(True))})
        if path.startswith("/api/chat/"):
            cid = path.split("/")[3]
            chat = w.chat(cid)
            return self._send(200, {"id": cid, "title": chat.get("title", ""), "n": len(chat["messages"]),
                                    "archived": bool(chat.get("archived")), "blocks": blocks(chat),
                                    "live": w.live(cid)})
        if path == "/api/file":
            p = Path(q.get("p", "")).resolve()
            if not p.is_relative_to(A.ATT_DIR.resolve()) or not p.is_file():
                return self._err(404, "Nicht gefunden")
            return self._send(200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                              {"Cache-Control": "private, max-age=86400"})
        if path == "/api/events":
            return self._events(q.get("chat", ""), int(q.get("seit", "0") or 0))
        return self._err(404, "Nicht gefunden")

    def _post(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/login":
            tok = str(self._json().get("token", "")).strip()
            if self._login(tok):
                return self._send(200, {"ok": True}, headers={"Set-Cookie": self._cookie(tok)})
            return self._err(401, "Schlüssel stimmt nicht")
        if not self._token_ok():
            return self._err(401, "Nicht angemeldet")
        w = self.web
        if path == "/api/select":
            w.select(self._json())
            return self._send(200, w.state())
        if path == "/api/send":
            d = self._json()
            cid = w.send(d.get("chat") or "", str(d.get("text", "")).strip(), d.get("atts") or [])
            return self._send(200, {"chat": cid})
        if path == "/api/upload":
            name = urllib.parse.unquote(self.headers.get("X-Name") or "datei")
            return self._send(200, w.upload(name, self._body()))
        if path == "/api/stt":
            data = self._body(25 * 1024 * 1024)
            return self._send(200, {"text": w.transcribe(data, self.headers.get("Content-Type") or "")})
        if path == "/api/tts":
            return self._send(200, w.tts(str(self._json().get("text", ""))), "audio/wav")
        parts = path.split("/")
        if len(parts) == 5 and parts[2] == "chat":
            cid, action = parts[3], parts[4]
            run = w.runs.get(cid)
            d = self._json()
            if action == "answer" and run and run.agent.pending:
                c = d.get("choice")
                run.agent.answer(c in ("yes", "always"), remember=(c == "always"), reason=str(d.get("reason", "")))
            elif action == "stop" and run:
                run.agent.stop()
            elif action == "meta":
                if cid in w.runs:
                    raise ValueError("Bitte warten, bis die Antwort fertig ist.")
                fields = {}
                if "title" in d:
                    fields["title"] = " ".join(str(d["title"]).split())[:80] or "Chat"
                if "archived" in d:
                    fields["archived"] = True if d["archived"] else None
                C.update_chat_meta(cid, **fields)
                if w.desktop:
                    w.desktop.saved(cid, meta=fields)
            elif action == "delete":
                if cid in w.runs:
                    raise ValueError("Bitte warten, bis die Antwort fertig ist.")
                C.delete_chat(cid)
                if w.desktop:
                    w.desktop.saved(cid, deleted=True)
            w.hub.bump()
            return self._send(200, {"ok": True})
        return self._err(404, "Nicht gefunden")

    def _static(self, name):
        f = STATIC / name
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "webmanifest": "application/manifest+json"}[name.rsplit(".", 1)[1]]
        self._send(200, f.read_bytes(), ctype)

    def _events(self, cid, since):
        """Server-Sent Events: Live-Zustand des offenen Chats + Benachrichtigungen."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        hub, w = self.web.hub, self.web
        last_live, last_list, seen = None, None, since
        ver = -1
        if not since:       # neue Seite: alte Meldungen nicht nochmal zeigen
            seen = max((n["id"] for n in list(hub.notes)), default=0)
            self.wfile.write(f"event: hello\ndata: {json.dumps({'last': seen})}\n\n".encode())
        while self.web.httpd is not None:
            with hub.cond:
                if hub.ver == ver:
                    hub.cond.wait(15)
                ver = hub.ver
            out = []
            notes = [n for n in list(hub.notes) if n["id"] > seen]
            for n in notes:
                out.append(("note", n))
                seen = n["id"]
            if cid:
                live = w.live(cid)
                if live != last_live:
                    out.append(("live", live))
                    last_live = live
            running = sorted(w.runs)
            if running != last_list:
                out.append(("runs", running))
                last_list = running
            data = "".join(f"event: {ev}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for ev, d in out) or ": ping\n\n"
            self.wfile.write(data.encode())
            self.wfile.flush()
            time.sleep(0.05)      # höchstens ~20 Updates pro Sekunde
