"""MCP (Model Context Protocol): externe Werkzeug-Server per stdio einbinden.

Ein Server wird in den Einstellungen mit Befehl eingetragen, z. B.
    uvx mcp-server-time
    npx -y @modelcontextprotocol/server-filesystem /home/senol/Dokumente
spike startet ihn im Hintergrund, fragt seine Werkzeuge ab (tools/list) und bietet sie dem Modell als
mcp_<server>_<werkzeug> an. Aufrufe gehen per tools/call an den Server.

Freigabe: Werkzeuge mit readOnlyHint laufen ohne Nachfrage, alle anderen über die Freigabe-Karte
(pro Server abschaltbar mit confirm = "nie").
"""
import itertools
import json
import os
import re
import shlex
import subprocess
import threading

PROTOCOL = "2025-06-18"
TOOLS: dict[str, dict] = {}       # spike-name -> {"server", "tool", "spec", "label", "readonly"}
SERVERS: dict[str, "Server"] = {}
STATUS: dict[str, str] = {}       # server-name -> Statustext für die Einstellungen
_lock = threading.Lock()


def _slug(text, n=24):
    return re.sub(r"[^a-zA-Z0-9_]", "_", text).strip("_")[:n] or "x"


class Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.proc = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.tools = []
        self.stderr_tail = []

    # --- Verbindung ---
    def start(self, timeout=60):
        cmd = self.cfg.get("command", "").strip()
        if not cmd:
            raise RuntimeError("kein Befehl eingetragen")
        args = shlex.split(os.path.expanduser(cmd))
        env = {**os.environ, **{k: v for k, v in (self.cfg.get("env") or {}).items() if k}}
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                                     start_new_session=True)
        threading.Thread(target=self._read, daemon=True, name=f"mcp-{self.name}").start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "spike", "version": "1.0"}}, timeout=timeout)
        self.notify("notifications/initialized")
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            tools += res.get("tools") or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        return tools

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # --- JSON-RPC ---
    def _send(self, obj):
        if not self.alive():
            raise RuntimeError(f"MCP-Server {self.name} läuft nicht" +
                               (f": {self.stderr_tail[-1]}" if self.stderr_tail else ""))
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method, params=None, timeout=120):
        rid = next(self._ids)
        slot = {}
        with self._cond:
            self._waiting[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        with self._cond:
            ok = self._cond.wait_for(lambda: "msg" in slot or not self.alive(), timeout=timeout)
            self._waiting.pop(rid, None)
        if "msg" not in slot:
            raise RuntimeError(f"{self.name}: keine Antwort auf {method}" +
                               ("" if ok else f" nach {timeout} s") +
                               (f" ({self.stderr_tail[-1]})" if self.stderr_tail else ""))
        msg = slot["msg"]
        if "error" in msg:
            e = msg["error"]
            raise RuntimeError(f"{self.name}: {e.get('message', e)}")
        return msg.get("result") or {}

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cond:
                    slot = self._waiting.get(msg["id"])
                    if slot is not None:
                        slot["msg"] = msg
                    self._cond.notify_all()
            elif "id" in msg and "method" in msg:     # Anfrage vom Server (z. B. ping) → leer beantworten
                try:
                    if msg["method"] == "ping":
                        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                    else:
                        self._send({"jsonrpc": "2.0", "id": msg["id"],
                                    "error": {"code": -32601, "message": "nicht unterstützt"}})
                except Exception:
                    pass
        with self._cond:
            self._cond.notify_all()

    def _read_err(self):
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]


# ---------- Verwaltung ----------

def start_all(server_cfgs: list[dict], on_done=None):
    """(Neu-)Start aller aktivierten Server im Hintergrund."""
    def job():
        stop_all()
        for cfg in server_cfgs or []:
            name = cfg.get("name") or "mcp"
            if not cfg.get("enabled", True):
                STATUS[name] = "aus"
                continue
            STATUS[name] = "startet …"
            srv = Server(cfg)
            try:
                tools = srv.start()
            except Exception as e:
                srv.stop()
                STATUS[name] = f"❌ {e}"
                continue
            with _lock:
                SERVERS[name] = srv
                for t in tools:
                    tname = f"mcp_{_slug(name, 16)}_{_slug(t['name'], 40)}"[:64]
                    ann = t.get("annotations") or {}
                    desc = (t.get("description") or t["name"]).strip()
                    TOOLS[tname] = {
                        "server": name, "tool": t["name"], "readonly": bool(ann.get("readOnlyHint")),
                        "label": f"{ann.get('title') or t['name']}  (MCP {name})",
                        "spec": {"type": "function", "function": {
                            "name": tname, "description": f"[{name}] {desc}"[:1000],
                            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}},
                    }
            STATUS[name] = f"✅ {len(tools)} Werkzeug(e)"
        if on_done:
            on_done()
    threading.Thread(target=job, daemon=True, name="mcp-start").start()


def stop_all():
    with _lock:
        for srv in SERVERS.values():
            srv.stop()
        SERVERS.clear()
        TOOLS.clear()


def call(tname: str, args: dict, confirm, server_cfgs, on_image=None) -> str:
    t = TOOLS[tname]
    cfg = next((c for c in server_cfgs or [] if c.get("name") == t["server"]), {})
    mode = cfg.get("confirm", "schreibend")
    if mode == "immer" or (mode == "schreibend" and not t["readonly"]):
        shown = json.dumps(args, ensure_ascii=False, indent=1)
        if not confirm(f"MCP-Werkzeug „{t['tool']}“ ({t['server']}) ausführen?\n\n{shown[:1500]}"):
            return "Vom Nutzer abgelehnt."
    srv = SERVERS.get(t["server"])
    if not srv or not srv.alive():
        return f"Fehler: MCP-Server {t['server']} läuft nicht ({STATUS.get(t['server'], '')})."
    res = srv.request("tools/call", {"name": t["tool"], "arguments": args or {}}, timeout=300)
    out = []
    for part in res.get("content") or []:
        kind = part.get("type")
        if kind == "text":
            out.append(part.get("text", ""))
        elif kind == "image" and on_image:
            on_image(part.get("data", ""), part.get("mimeType", "image/png"))
            out.append("[Bild – folgt als nächste Nachricht]")
        elif kind == "resource":
            r = part.get("resource") or {}
            out.append(r.get("text") or f"[Ressource {r.get('uri', '')}]")
        else:
            out.append(f"[{kind}]")
    if res.get("structuredContent") and not out:
        out.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    text = "\n".join(out) or "(leer)"
    return ("Fehler vom Werkzeug: " + text) if res.get("isError") else text
