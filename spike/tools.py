"""Werkzeuge für den Agenten. Ausgaben werden hart begrenzt, um Kontext zu sparen."""
import html
import json
import re
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from . import plugins

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _fn(name, desc, props=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props or {}, "required": required or []},
        },
    }


TOOL_SPECS = {
    "web_search": _fn("web_search", "Websuche (DuckDuckGo). Liefert Titel, URL, Kurztext.",
                      {"query": {"type": "string"}}, ["query"]),
    "fetch_url": _fn("fetch_url", "Lädt eine Webseite schnell (ohne JavaScript) und gibt den Text zurück.",
                     {"url": {"type": "string"},
                      "offset": {"type": "integer", "description": "Startzeichen für weiteres Lesen"}}, ["url"]),
    "browser_open": _fn("browser_open", "Öffnet eine URL im echten Browser (mit JavaScript). Liefert Text und "
                        "nummerierte interaktive Elemente.", {"url": {"type": "string"}}, ["url"]),
    "browser_read": _fn("browser_read", "Liest den Text der aktuellen Browserseite (ab offset).",
                        {"offset": {"type": "integer"}}),
    "browser_click": _fn("browser_click", "Klickt Element [id] der aktuellen Seite.",
                         {"id": {"type": "integer"}}, ["id"]),
    "browser_type": _fn("browser_type", "Tippt Text in Eingabefeld [id], optional mit Enter.",
                        {"id": {"type": "integer"}, "text": {"type": "string"},
                         "submit": {"type": "boolean"}}, ["id", "text"]),
    "browser_back": _fn("browser_back", "Geht im Browser eine Seite zurück."),
    "run_shell": _fn("run_shell", "Führt einen Shell-Befehl aus (Nutzer muss bestätigen).",
                     {"command": {"type": "string"}}, ["command"]),
    "read_file": _fn("read_file", "Liest eine Textdatei (ab offset).",
                     {"path": {"type": "string"}, "offset": {"type": "integer"}}, ["path"]),
    "list_dir": _fn("list_dir", "Listet ein Verzeichnis auf.", {"path": {"type": "string"}}, ["path"]),
    "write_file": _fn("write_file", "Schreibt eine Textdatei (Nutzer muss bestätigen).",
                      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
}

TOOL_LABELS = {
    "web_search": "Websuche", "fetch_url": "Seite laden (schnell)", "browser_open": "Browser: öffnen",
    "browser_read": "Browser: lesen", "browser_click": "Browser: klicken", "browser_type": "Browser: tippen",
    "browser_back": "Browser: zurück", "run_shell": "Shell-Befehl ⚠", "read_file": "Datei lesen",
    "list_dir": "Ordner auflisten", "write_file": "Datei schreiben ⚠",
}


def clip(text: str, limit: int, offset: int = 0) -> str:
    offset = max(0, offset or 0)
    part = text[offset:offset + limit]
    rest = len(text) - offset - len(part)
    if rest > 0:
        part += f"\n[... {rest} weitere Zeichen – zum Weiterlesen offset={offset + len(part)}]"
    return part


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "footer", "ul", "ol", "table", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str) -> str:
    p = _Text()
    p.feed(src)
    t = "".join(p.out)
    t = re.sub(r"[ \t\r\f\v ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def http_get(url: str, timeout=20, headers: dict | None = None) -> str:
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")   # Umlaute/Leerzeichen erlauben
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de,en;q=0.8", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(3_000_000)
        charset = r.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, "replace")


# ---------- Browser (Playwright, ein fester Thread) ----------

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));
  const els = document.querySelectorAll('a[href],button,input,textarea,select,[role=button],[role=link],[onclick]');
  const out = []; let n = 0;
  for (const e of els) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (e.type === 'hidden') continue;
    n++; e.setAttribute('data-agent-id', n);
    let label = (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || e.title || e.name || '')
      .trim().replace(/\s+/g, ' ').slice(0, 60);
    let tag = e.tagName.toLowerCase();
    if (tag === 'input') tag = 'input:' + (e.type || 'text');
    out.push('[' + n + '] ' + tag + ' ' + label);
    if (n >= 120) break;
  }
  return {elements: out.join('\n'), text: (document.body ? document.body.innerText : '')};
}
"""


class Browser:
    def __init__(self):
        self.ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
        self.pw = self.browser = self.page = None
        self.headless = True
        self._launched_headless = None
        self.last_text = ""

    def _ensure(self):
        from playwright.sync_api import sync_playwright
        if self.browser and self._launched_headless != self.headless:
            self.browser.close()
            self.browser = self.page = None
        if not self.pw:
            self.pw = sync_playwright().start()
        if not self.browser or not self.browser.is_connected():
            self.browser = self.pw.chromium.launch(headless=self.headless)
            self._launched_headless = self.headless
            self.page = None
        if not self.page or self.page.is_closed():
            self.page = self.browser.new_page(user_agent=UA, locale="de-DE")

    def call(self, fn, *args):
        def job():
            self._ensure()
            return fn(*args)
        return self.ex.submit(job).result(timeout=120)

    def _snapshot(self, limit):
        p = self.page
        p.wait_for_timeout(700)
        snap = p.evaluate(SNAPSHOT_JS)
        self.last_text = re.sub(r"\n\s*\n+", "\n\n", snap["text"]).strip()
        head = f"Titel: {p.title()}\nURL: {p.url}\n--- Elemente ---\n{snap['elements']}\n--- Text ---\n"
        return head + clip(self.last_text, max(500, limit - len(head)))

    def open(self, url, limit):
        if not re.match(r"^https?://", url):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return self._snapshot(limit)

    def read(self, offset, limit):
        return clip(self.last_text, limit, offset)

    def click(self, eid, limit):
        self.page.click(f'[data-agent-id="{int(eid)}"]', timeout=10000)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        return self._snapshot(limit)

    def type(self, eid, text, submit, limit):
        sel = f'[data-agent-id="{int(eid)}"]'
        self.page.fill(sel, text, timeout=10000)
        if submit:
            self.page.press(sel, "Enter")
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
        return self._snapshot(limit)

    def back(self, limit):
        self.page.go_back(timeout=15000)
        return self._snapshot(limit)

    def shutdown(self):
        def job():
            try:
                if self.browser:
                    self.browser.close()
                if self.pw:
                    self.pw.stop()
            except Exception:
                pass
        try:
            self.ex.submit(job).result(timeout=10)
        except Exception:
            pass
        self.ex.shutdown(wait=False)


# ---------- Werkzeugkasten ----------

class ToolBox:
    def __init__(self, settings: dict, browser: Browser):
        self.settings = settings
        self.browser = browser

    @property
    def limit(self):
        return int(self.settings.get("tool_output_max_chars", 6000))

    def run(self, name: str, args: dict, confirm) -> str:
        try:
            if name in plugins.PLUGINS:
                return clip(plugins.call(name, args, self, confirm), self.limit)
            fn = getattr(self, "t_" + name, None)
            if not fn:
                return f"Fehler: unbekanntes Werkzeug '{name}'"
            return fn(confirm=confirm, **args)
        except TypeError as e:
            return f"Fehler: falsche Argumente: {e}"
        except Exception as e:
            return f"Fehler: {type(e).__name__}: {e}"

    def t_web_search(self, query, confirm=None):
        q = urllib.parse.quote_plus(query)
        page = http_get(f"https://html.duckduckgo.com/html/?q={q}&kl=de-de")
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
        out = []
        for i, (href, title) in enumerate(links[:8]):
            m = re.search(r"uddg=([^&]+)", href)
            url = urllib.parse.unquote(m.group(1)) if m else html.unescape(href)
            strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            snip = strip(snippets[i]) if i < len(snippets) else ""
            out.append(f"{i + 1}. {strip(title)}\n   {url}\n   {snip[:200]}")
        return "\n".join(out) if out else "Keine Ergebnisse (evtl. blockiert die Suchmaschine – versuche browser_open)."

    def t_fetch_url(self, url, offset=0, confirm=None):
        if not re.match(r"^https?://", url):
            url = "https://" + url
        return clip(html_to_text(http_get(url)), self.limit, offset)

    def t_browser_open(self, url, confirm=None):
        self.browser.headless = bool(self.settings.get("browser_headless", True))
        return self.browser.call(self.browser.open, url, self.limit)

    def t_browser_read(self, offset=0, confirm=None):
        return self.browser.call(self.browser.read, offset, self.limit)

    def t_browser_click(self, id, confirm=None):
        return self.browser.call(self.browser.click, id, self.limit)

    def t_browser_type(self, id, text, submit=False, confirm=None):
        return self.browser.call(self.browser.type, id, text, submit, self.limit)

    def t_browser_back(self, confirm=None):
        return self.browser.call(self.browser.back, self.limit)

    def t_run_shell(self, command, confirm=None):
        if not confirm(f"Shell-Befehl ausführen?\n\n{command}"):
            return "Vom Nutzer abgelehnt."
        p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=180, cwd=Path.home())
        return clip(f"exit={p.returncode}\n{p.stdout}{p.stderr}", self.limit)

    def t_read_file(self, path, offset=0, confirm=None):
        return clip(Path(path).expanduser().read_text("utf-8", "replace"), self.limit, offset)

    def t_list_dir(self, path, confirm=None):
        p = Path(path).expanduser()
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"{'📁' if i.is_dir() else '  '} {i.name}" for i in items[:300]]
        return clip("\n".join(lines) or "(leer)", self.limit)

    def t_write_file(self, path, content, confirm=None):
        p = Path(path).expanduser()
        if not confirm(f"Datei schreiben?\n\n{p}\n({len(content)} Zeichen{', ÜBERSCHREIBT' if p.exists() else ''})"):
            return "Vom Nutzer abgelehnt."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"Geschrieben: {p} ({len(content)} Zeichen)"


def specs_for(names):
    out = []
    for n in names:
        if n in TOOL_SPECS:
            out.append(TOOL_SPECS[n])
        elif n in plugins.PLUGINS:
            out.append(plugins.PLUGINS[n]["spec"])
    return out


def all_tools():
    """[(name, anzeigename)] – eingebaute Werkzeuge und geladene Plugins."""
    return [(n, TOOL_LABELS.get(n, n)) for n in TOOL_SPECS] + \
           [(n, p["label"]) for n, p in plugins.PLUGINS.items()]


def load_plugins():
    return plugins.load(set(TOOL_SPECS))


def short_args(arguments: str, n=120) -> str:
    try:
        a = json.loads(arguments or "{}")
        s = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in a.items())
    except Exception:
        s = arguments or ""
    return s if len(s) <= n else s[:n] + "…"
