"""Werkzeuge für den Agenten. Ausgaben werden hart begrenzt, um Kontext zu sparen."""
import html
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

from . import memory, plugins

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
    "run_shell": _fn("run_shell", "Führt einen bash-Befehl aus (Nutzer bestätigt). Kein interaktives Programm "
                     "(kein vim/less). sudo geht: der Nutzer gibt das Passwort selbst in einem Fenster ein. "
                     "„cd“ bleibt für folgende Befehle erhalten.",
                     {"command": {"type": "string"}}, ["command"]),
    "read_file": _fn("read_file", "Liest eine Textdatei mit Zeilennummern (Format „Nummer│Text“; die Nummern "
                     "gehören nicht zum Inhalt). Lange Dateien abschnittsweise mit line lesen.",
                     {"path": {"type": "string"}, "line": {"type": "integer", "description": "erste Zeile, Standard 1"},
                      "count": {"type": "integer", "description": "Anzahl Zeilen, Standard 400"}}, ["path"]),
    "edit_file": _fn("edit_file", "Ändert eine Textdatei gezielt: ersetzt old (exakt wie in der Datei, inkl. "
                     "Einrückung, ohne Zeilennummern) durch new. old muss eindeutig sein, sonst mehr Kontext angeben "
                     "oder all=true. Für neue Dateien write_file. Nutzer bestätigt mit Diff.",
                     {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                      "all": {"type": "boolean", "description": "alle Vorkommen ersetzen"}}, ["path", "old", "new"]),
    "grep": _fn("grep", "Durchsucht Dateien nach Text/Regex (ripgrep). Liefert Datei:Zeile: Treffer.",
                {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Ordner/Datei, Standard: aktueller Ordner"},
                 "glob": {"type": "string", "description": "z. B. *.py"}}, ["pattern"]),
    "list_dir": _fn("list_dir", "Listet ein Verzeichnis auf.", {"path": {"type": "string"}}, ["path"]),
    "write_file": _fn("write_file", "Schreibt eine Textdatei (Nutzer muss bestätigen).",
                      {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    "find_files": _fn("find_files", "Sucht Dateien/Ordner nach Namen auf dem PC (schnell, ohne Bestätigung). "
                      "Ohne path: Systemindex + Home-Ordner, und wenn dort nichts ist, automatisch alle Laufwerke "
                      "(/mnt, USB) – ein Aufruf genügt. "
                      "Mit path (z. B. /mnt/nas_storage): sucht frisch nur dort. "
                      "name = Teil des Namens (Groß/klein egal) oder Muster wie *.srt.",
                      {"name": {"type": "string"},
                       "path": {"type": "string", "description": "optional: nur hier suchen"}}, ["name"]),
    "screenshot": _fn("screenshot", "Macht ein Bildschirmfoto (ganzer Bildschirm oder aktives Fenster) und zeigt es "
                      "dir als Bild. Nur nutzen, wenn du sehen musst, was auf dem Bildschirm ist.",
                      {"window": {"type": "boolean", "description": "nur das aktive Fenster"}}),
    "schedule_add": _fn("schedule_add", "Plant eine Erinnerung (Benachrichtigung zur Zeit) oder eine Aufgabe (spike "
                        "fragt dich zur Zeit selbst mit text und meldet das Ergebnis; ohne Freigaben, also nur lesen/"
                        "suchen/rechnen). when: 'in 20 min', '15:30', 'morgen 8:00', 'freitag 18:00', '2026-11-03 09:00'. "
                        "repeat (optional): täglich, werktags, wöchentlich, stündlich, 'alle 30 min'.",
                        {"when": {"type": "string"}, "text": {"type": "string", "description": "Erinnerungstext bzw. "
                                                              "Auftrag an dich selbst"},
                         "kind": {"type": "string", "enum": ["erinnerung", "aufgabe"]},
                         "repeat": {"type": "string"}}, ["when", "text"]),
    "schedule_list": _fn("schedule_list", "Zeigt geplante Erinnerungen und Aufgaben."),
    "schedule_delete": _fn("schedule_delete", "Löscht eine geplante Erinnerung/Aufgabe per id.",
                           {"id": {"type": "string"}}, ["id"]),
    "python_run": _fn("python_run", "Führt Python-3-Code aus (Nutzer bestätigt) und gibt stdout/stderr zurück. "
                      "Für Rechnen, Daten auswerten, Dateien umwandeln. Ergebnisse mit print() ausgeben.",
                      {"code": {"type": "string"}}, ["code"]),
    "pc": _fn("pc", "Steuert den Linux-PC (KDE). Aktionen: open (Datei/Ordner/URL/Programm öffnen, value=Pfad oder "
              "URL), clipboard_get, clipboard_set (value=Text), notify (value=Text), volume (value=leer zum Abfragen, "
              "50 / +5 / -5 / mute), media (value=play-pause|next|previous|stop|status).",
              {"action": {"type": "string", "enum": ["open", "clipboard_get", "clipboard_set", "notify",
                                                     "volume", "media"]},
               "value": {"type": "string"}}, ["action"]),
    "memory_save": _fn("memory_save", "Merkt sich dauerhaft einen kurzen Fakt über den Nutzer/seine Systeme "
                       "(ein Satz). Nur Wichtiges, das in späteren Chats nützt; nichts Vertrauliches.",
                       {"fact": {"type": "string"}}, ["fact"]),
    "memory_search": _fn("memory_search", "Durchsucht das Gedächtnis nach Stichworten (leer = neueste).",
                         {"query": {"type": "string"}}),
    "memory_delete": _fn("memory_delete", "Löscht einen Gedächtnis-Eintrag per id (z. B. wenn er falsch ist).",
                         {"id": {"type": "string"}}, ["id"]),
}

MEMORY_TOOLS = ["memory_save", "memory_search", "memory_delete"]

TOOL_LABELS = {
    "web_search": "Websuche", "fetch_url": "Seite laden (schnell)", "browser_open": "Browser: öffnen",
    "browser_read": "Browser: lesen", "browser_click": "Browser: klicken", "browser_type": "Browser: tippen",
    "browser_back": "Browser: zurück", "run_shell": "Shell-Befehl ⚠", "read_file": "Datei lesen",
    "list_dir": "Ordner auflisten", "find_files": "Dateien suchen", "edit_file": "Datei ändern (Diff) ⚠",
    "grep": "In Dateien suchen (grep)", "screenshot": "Screenshot ansehen",
    "schedule_add": "Erinnerung/Aufgabe planen", "schedule_list": "Geplantes anzeigen",
    "schedule_delete": "Geplantes löschen", "write_file": "Datei schreiben ⚠", "python_run": "Python ausführen ⚠",
    "pc": "PC steuern (öffnen, Zwischenablage, Lautstärke, Medien)", "memory_save": "Gedächtnis: merken",
    "memory_search": "Gedächtnis: suchen", "memory_delete": "Gedächtnis: löschen",
}


def make_diff(old: str, new: str, name: str, context=3, max_lines=400) -> str:
    """Unified Diff (für Freigabe-Karte und Chat)."""
    import difflib
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}",
                                      n=context, lineterm=""))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… {len(lines) - max_lines} weitere Diff-Zeilen"]
    return "\n".join(lines)


def clip_tail(text: str, limit: int) -> str:
    """Für Befehlsausgaben: Anfang und vor allem das Ende behalten (dort stehen meist die Fehler)."""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return text[:head] + f"\n[... {len(text) - limit} Zeichen in der Mitte ausgelassen ...]\n" + text[-tail:]


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


# ---------- Laufwerks-Index (plocate-Datenbank für /mnt, ohne root) ----------

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "spike"
DRIVE_INDEX = CACHE_DIR / "mnt.db"
DRIVE_ROOT = "/mnt"
_index_lock = __import__("threading").Lock()


def drive_index_age() -> float | None:
    """Alter des Index in Stunden (None = gibt es nicht)."""
    try:
        import time as _t
        return (_t.time() - DRIVE_INDEX.stat().st_mtime) / 3600
    except OSError:
        return None


def refresh_drive_index(max_age_h=24, wait=False) -> str:
    """Erneuert den Index im Hintergrund (niedrige Priorität), wenn er älter als max_age_h ist."""
    age = drive_index_age()
    if age is not None and age < max_age_h:
        return f"Index ist aktuell ({age:.1f} h alt)."
    if not shutil.which("updatedb") or not Path(DRIVE_ROOT).is_dir():
        return "updatedb oder /mnt fehlt – kein Laufwerks-Index."
    if not _index_lock.acquire(blocking=False):
        return "Index wird gerade schon erstellt."

    def job():
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DRIVE_INDEX.with_suffix(".tmp")
            cmd = ["updatedb", "-l", "no", "-U", DRIVE_ROOT, "-o", str(tmp), "--prunefs", "", "--prunepaths", "",
                   "--prunenames", ".snapshots .git node_modules .cache"]
            if shutil.which("ionice"):
                cmd = ["ionice", "-c3"] + cmd
            cmd = ["nice", "-n", "19"] + cmd
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
            if p.returncode == 0 and tmp.exists():
                tmp.replace(DRIVE_INDEX)
            else:
                print("Laufwerks-Index fehlgeschlagen:", p.stderr[-500:])
        except Exception as e:
            print("Laufwerks-Index fehlgeschlagen:", e)
        finally:
            _index_lock.release()
    if wait:
        job()
        return "Index erstellt."
    __import__("threading").Thread(target=job, daemon=True, name="drive-index").start()
    return "Index wird im Hintergrund erstellt."


# ---------- sudo: Passwort über ein KDE-Fenster (geht direkt an sudo, nie an das Modell) ----------

def _askpass() -> str:
    tool = shutil.which("kdialog") or shutil.which("zenity")
    if not tool:
        return ""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / "askpass.sh"
    if "kdialog" in tool:
        body = 'exec kdialog --title "spike – Administrator-Rechte" --password "${1:-Passwort für sudo:}"'
    else:
        body = 'exec zenity --password --title "spike – Administrator-Rechte"'
    text = f"#!/bin/sh\n# von spike erzeugt: fragt das sudo-Passwort grafisch ab\n{body}\n"
    if not f.exists() or f.read_text() != text:
        f.write_text(text)
        f.chmod(0o700)
    return str(f)


# ---------- Werkzeugkasten ----------

class ToolBox:
    def __init__(self, settings: dict, browser: Browser):
        self.settings = settings
        self.browser = browser
        self.cwd = Path.home()        # merkt sich „cd“ zwischen Befehlen (Modell und !-Befehle)
        self.on_output = None         # fn(text) – Live-Ausgabe laufender Befehle
        self.last_diff = ""           # Diff der letzten Dateiänderung (Anzeige im Chat)
        self.vision = False           # versteht das Modell Bilder?
        self._images = []             # Bilder aus Werkzeugen, die als nächste Nachricht ans Modell gehen

    def take_images(self):
        imgs, self._images = self._images, []
        return imgs

    def t_screenshot(self, window=False, confirm=None):
        from . import attachments
        if not self.vision:
            return ("Das aktuelle Modell kann keine Bilder sehen – Screenshot nicht möglich. Sag dem Nutzer, "
                    "dass dafür ein Modell mit Bildverständnis (Vision) nötig ist.")
        att = attachments.screenshot(active_window=bool(window))
        self._images.append(att)
        return "Screenshot aufgenommen – das Bild folgt als nächste Nachricht."
        self._proc = None

    # --- Shell mit Live-Ausgabe ---
    _CWD_MARK = "\x1eSPIKE_CWD:"

    def shell(self, command, timeout=None, on_output=None):
        """Führt einen Befehl in bash aus. Gibt (exit_code, ausgabe) zurück; ausgabe läuft live an on_output."""
        import signal
        import threading as _th
        timeout = timeout or int(self.settings.get("shell_timeout", 300))
        askpass = _askpass()
        prelude = 'sudo() { command sudo -A "$@"; }\n' if askpass else ""
        script = (f"{prelude}{command}\n__spike_rc=$?\nprintf '{self._CWD_MARK}%s\\n' \"$PWD\"\nexit $__spike_rc")
        cwd = self.cwd if Path(self.cwd).is_dir() else Path.home()
        env = {**os.environ, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat",
               "NO_COLOR": "1", **({"SUDO_ASKPASS": askpass} if askpass else {})}
        p = subprocess.Popen(["bash", "-c", script], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
                             start_new_session=True, env=env)
        self._proc = p
        out, new_cwd = [], [None]

        def reader():
            for line in p.stdout:
                i = line.find(self._CWD_MARK)
                if i >= 0:     # Markierung kann an einer Zeile ohne Zeilenende hängen
                    new_cwd[0] = line[i + len(self._CWD_MARK):].strip()
                    line = line[:i]
                    if not line:
                        continue
                out.append(line)
                if on_output:
                    on_output(line)
        t = _th.Thread(target=reader, daemon=True)
        t.start()
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        t.join(timeout=2)
        self._proc = None
        if new_cwd[0] and Path(new_cwd[0]).is_dir():
            self.cwd = Path(new_cwd[0])
        text = "".join(out).rstrip("\n")
        if timed_out:
            text += f"\n[abgebrochen nach {timeout} s]"
        return p.returncode, text

    def kill(self):
        import signal
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

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
        rc, out = self.shell(command, on_output=self.on_output)
        return f"exit={rc} · Ordner: {self.cwd}\n" + clip_tail(out, self.limit)

    def _path(self, path) -> Path:
        """Relative Pfade gelten ab dem aktuellen Ordner (cd)."""
        p = Path(str(path)).expanduser()
        return p if p.is_absolute() else Path(self.cwd) / p

    def t_read_file(self, path, line=1, count=400, offset=None, confirm=None):
        p = self._path(path)
        text = p.read_text("utf-8", "replace")
        if offset:                        # alte Variante (Zeichen-Offset)
            return clip(text, self.limit, offset)
        lines = text.split("\n")
        start = max(1, int(line or 1))
        count = max(1, min(int(count or 400), 2000))
        width = len(str(min(len(lines), start + count)))
        out, used = [], 0
        for i in range(start, min(len(lines), start + count - 1) + 1):
            row = f"{i:>{width}}│{lines[i - 1]}"
            if used + len(row) > self.limit:
                out.append(f"[… gekürzt – weiter mit line={i}]")
                break
            out.append(row)
            used += len(row) + 1
        else:
            if start + count - 1 < len(lines):
                out.append(f"[… {len(lines) - (start + count - 1)} weitere Zeilen – weiter mit line={start + count}]")
        return f"{p} ({len(lines)} Zeilen)\n" + "\n".join(out)

    def t_edit_file(self, path, old, new, all=False, confirm=None):
        p = self._path(path)
        if not p.exists():
            return f"Fehler: {p} gibt es nicht (neue Dateien mit write_file)."
        text = p.read_text("utf-8")
        n = text.count(old) if old else 0
        if n == 0:
            # häufiger Fehler: Zeilennummern oder Einrückung mitkopiert
            hint = " (Zeilennummern „12│“ gehören nicht dazu)" if re.search(r"^\s*\d+│", old, re.M) else ""
            return f"Fehler: old kommt in {p.name} nicht vor{hint}. Datei erneut lesen und exakt kopieren."
        if n > 1 and not all:
            return f"Fehler: old kommt {n}× vor. Mehr Kontext angeben oder all=true."
        updated = text.replace(old, new) if all else text.replace(old, new, 1)
        diff = make_diff(text, updated, str(p))
        if not confirm(f"Datei ändern?\n\n{p}", diff=diff):
            return "Vom Nutzer abgelehnt."
        p.write_text(updated, "utf-8")
        self.last_diff = diff
        first = text[:text.index(old)].count("\n") + 1
        return f"OK: {n if all else 1} Stelle(n) in {p} geändert (ab Zeile {first})."

    def t_grep(self, pattern, path="", glob="", confirm=None):
        root = self._path(path or ".")
        if shutil.which("rg"):
            args = ["rg", "-n", "--no-heading", "--color", "never", "-S", "--max-columns", "300",
                    "--max-count", "50", "-e", pattern]
            if glob:
                args += ["-g", glob]
        else:
            args = ["grep", "-rnI", "-E", pattern] + ([f"--include={glob}"] if glob else [])
        try:
            p = subprocess.run(args + [str(root)], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "Abgebrochen nach 60 s – Ordner eingrenzen."
        lines = p.stdout.splitlines()
        if not lines:
            return f"Keine Treffer für „{pattern}“ in {root}." + (f" {p.stderr.strip()[:200]}" if p.stderr else "")
        home = str(root) + "/"
        lines = [l[len(home):] if l.startswith(home) else l for l in lines]
        head = f"{len(lines)} Treffer in {root}" + (" (max. 50 pro Datei)" if len(lines) >= 50 else "") + ":"
        return clip("\n".join([head] + lines[:300]), self.limit)

    def t_list_dir(self, path, confirm=None):
        p = self._path(path)
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        lines = [f"{'📁' if i.is_dir() else '  '} {i.name}" for i in items[:300]]
        return clip("\n".join(lines) or "(leer)", self.limit)

    def t_write_file(self, path, content, confirm=None):
        p = self._path(path)
        old = p.read_text("utf-8", "replace") if p.exists() else ""
        diff = make_diff(old, content, str(p))
        what = "Datei überschreiben?" if p.exists() else "Neue Datei anlegen?"
        if not confirm(f"{what}\n\n{p} ({len(content)} Zeichen)", diff=diff):
            return "Vom Nutzer abgelehnt."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        self.last_diff = diff
        return f"Geschrieben: {p} ({len(content)} Zeichen)"

    # --- Dateisuche ---
    SKIP_PARTS = ("/.snapshots/", "/proc/", "/sys/", "/.git/", "/__pycache__/", "/.cache/", "/node_modules/",
                  "/.local/share/Trash/")

    def t_find_files(self, name, path="", confirm=None):
        name = (name or "").strip()
        if not name:
            return "Fehler: name fehlt."
        glob = any(ch in name for ch in "*?[")
        found, notes, seen = [], [], set()

        def add(lines):
            for line in lines:
                line = line.strip()
                if not line or line in seen or any(x in line + "/" for x in self.SKIP_PARTS):
                    continue
                seen.add(line)
                found.append(line)

        def run_fd(root, limit, timeout):
            if not shutil.which("fd"):
                return []
            args = ["fd", "-H", "-i", "--max-results", str(limit), "-E", ".snapshots", "-E", ".git",
                    "-E", "node_modules", "-E", ".cache"]
            args += ["--glob", name] if glob else ["-F", name]
            try:
                p = subprocess.run(args + [str(root)], capture_output=True, text=True, timeout=timeout)
                return p.stdout.splitlines()
            except subprocess.TimeoutExpired as e:
                notes.append(f"Suche in {root} nach {timeout} s abgebrochen (Teilergebnis).")
                out = e.stdout or b""
                return (out.decode("utf-8", "replace") if isinstance(out, bytes) else out).splitlines()

        if path:
            root = Path(path).expanduser()
            if not root.exists():
                return f"Fehler: {root} gibt es nicht."
            add(run_fd(root, 300, 40))
            src = f"frische Suche in {root}"
        else:
            has_index = DRIVE_INDEX.exists()
            if shutil.which("plocate"):
                dbs = [[]] + ([["-d", str(DRIVE_INDEX)]] if has_index else [])
                for db in dbs:
                    try:
                        p = subprocess.run(["plocate", *db, "-i", "-b", "-l", "400", name], capture_output=True,
                                           text=True, timeout=15)
                        add(l for l in p.stdout.splitlines() if os.path.lexists(l))   # Index kann veraltet sein
                    except subprocess.TimeoutExpired:
                        notes.append("plocate antwortete nicht.")
            add(run_fd(Path.home(), 200, 20))
            src = "Systemindex + Home-Ordner"
            if has_index:
                age = drive_index_age() or 0
                src += f" + Laufwerks-Index /mnt (Stand vor {age:.0f} h; ganz neue Dateien dort ggf. mit path suchen)"
            usb = Path("/run/media") / os.environ.get("USER", "")
            if usb.is_dir():
                for d in usb.iterdir():
                    add(run_fd(d, 100, 15))

        if not found and not path and not DRIVE_INDEX.exists():
            # Nichts im Index/Home → alle weiteren Laufwerke (/mnt, USB) gleichzeitig durchsuchen
            drives = [d for base in (Path("/mnt"), Path("/run/media") / os.environ.get("USER", ""))
                      if base.is_dir() for d in sorted(base.iterdir()) if d.is_dir()][:10]
            if drives and shutil.which("fd"):
                self._search_drives(drives, name, glob, add, notes)
                src += " + Laufwerke " + ", ".join(str(d) for d in drives)
        if not found:
            return f"Nichts gefunden für „{name}“ ({src})." + (" " + " ".join(notes) if notes else "")

        base = name.lower().strip("*")
        def rank(pth):
            b = os.path.basename(pth.rstrip("/")).lower()
            return (b != name.lower(), not b.startswith(base), pth.count("/"), len(pth))
        found.sort(key=rank)
        shown = []
        for pth in found[:40]:
            try:
                st = os.stat(pth)
                if os.path.isdir(pth):
                    info = "Ordner"
                else:
                    size = st.st_size
                    unit = next(u for u, lim in (("B", 1 << 10), ("KB", 1 << 20), ("MB", 1 << 30), ("GB", 1 << 62))
                                if size < lim)
                    div = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30}[unit]
                    info = f"{size / div:.0f} {unit}" if unit != "B" else f"{size} B"
                info += ", " + __import__("time").strftime("%d.%m.%Y", __import__("time").localtime(st.st_mtime))
            except OSError:
                info = "?"
            shown.append(f"{pth}  ({info})")
        head = f"{len(found)} Treffer ({src})" + (f", die ersten {len(shown)}:" if len(found) > len(shown) else ":")
        return clip("\n".join([head] + shown + notes), self.limit)

    def _search_drives(self, drives, name, glob, add, notes):
        """fd auf allen Laufwerken gleichzeitig. Sobald es Treffer gibt, noch kurz auf die anderen warten,
        dann abbrechen – große Laufwerke sollen eine gefundene Datei nicht aufhalten."""
        import time as _t
        args = ["fd", "-H", "-i", "--max-results", "100", "-E", ".snapshots", "-E", ".git", "-E", "node_modules"]
        args += ["--glob", name] if glob else ["-F", name]
        procs = {d: subprocess.Popen(args + [str(d)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                 for d in drives}
        start = _t.monotonic()
        deadline = start + int(self.settings.get("find_drive_timeout", 45))
        grace = None
        try:
            while procs:
                for d, p in list(procs.items()):
                    if p.poll() is not None:
                        lines = p.stdout.read().splitlines()
                        add(lines)
                        if lines and grace is None:
                            grace = _t.monotonic() + 3
                        del procs[d]
                now = _t.monotonic()
                if procs and now > min(deadline, grace or deadline):
                    break
                _t.sleep(0.1)
        finally:
            for p in procs.values():
                p.kill()
                p.wait()
        if procs:
            notes.append("Nicht fertig durchsucht (sehr groß, nach "
                         f"{_t.monotonic() - start:.0f} s abgebrochen): " + ", ".join(str(d) for d in procs) +
                         " – bei Bedarf mit path gezielt dort suchen.")

    # --- Python ---
    def t_python_run(self, code, confirm=None):
        if not confirm(f"Python-Code ausführen?\n\n{code[:3000]}"):
            return "Vom Nutzer abgelehnt."
        work = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "spike" / "python"
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".py", dir=work, delete=False, encoding="utf-8") as f:
            f.write(code)
        try:
            timeout = int(self.settings.get("python_timeout", 120))
            p = subprocess.run([sys.executable, "-I", f.name], capture_output=True, text=True,
                               timeout=timeout, cwd=work)
            out = f"exit={p.returncode}\n{p.stdout}{p.stderr}"
        except subprocess.TimeoutExpired:
            out = f"Abgebrochen: länger als {self.settings.get('python_timeout', 120)} s."
        finally:
            Path(f.name).unlink(missing_ok=True)
        return clip(out.strip() + f"\n(Arbeitsordner: {work})", self.limit)

    # --- PC-Steuerung ---
    @staticmethod
    def _cmd(args, input_text=None, timeout=10):
        p = subprocess.run(args, capture_output=True, text=True, input=input_text, timeout=timeout)
        return (p.stdout + p.stderr).strip(), p.returncode

    def t_pc(self, action, value="", confirm=None):
        value = (value or "").strip()
        if action == "open":
            if not value:
                return "Fehler: value (Pfad oder URL) fehlt."
            target = value if re.match(r"^[a-z]+://", value) else str(Path(value).expanduser())
            if not confirm(f"Öffnen?\n\n{target}"):
                return "Vom Nutzer abgelehnt."
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return f"Geöffnet: {target}"
        if action == "clipboard_get":
            out, rc = self._cmd(["wl-paste", "--no-newline"])
            return clip(out, self.limit) if rc == 0 else "Zwischenablage ist leer oder enthält keinen Text."
        if action == "clipboard_set":
            subprocess.run(["wl-copy"], input=value, text=True, timeout=5)
            return f"In die Zwischenablage kopiert ({len(value)} Zeichen)."
        if action == "notify":
            self._cmd(["notify-send", "-a", "spike", "-i", "spike", "spike", value or "…"])
            return "Benachrichtigung gezeigt."
        if action == "volume":
            sink = "@DEFAULT_AUDIO_SINK@"
            if value == "mute":
                self._cmd(["wpctl", "set-mute", sink, "toggle"])
            elif re.fullmatch(r"[+-]\d{1,3}", value):
                self._cmd(["wpctl", "set-volume", "-l", "1.0", sink, f"{value[1:]}%{value[0]}"])
            elif re.fullmatch(r"\d{1,3}", value):
                self._cmd(["wpctl", "set-volume", "-l", "1.0", sink, f"{min(100, int(value))}%"])
            elif value:
                return "Fehler: volume erwartet leer, eine Zahl 0–100, +N, -N oder mute."
            out, _ = self._cmd(["wpctl", "get-volume", sink])
            m = re.search(r"([\d.]+)", out)
            pct = f"{round(float(m.group(1)) * 100)} %" if m else out
            return f"Lautstärke: {pct}" + (" (stumm)" if "MUTED" in out else "")
        if action == "media":
            return self._media(value or "status")
        return f"Fehler: unbekannte Aktion '{action}'."

    def _media(self, what):
        qd = shutil.which("qdbus6") or shutil.which("qdbus")
        if not qd:
            return "Fehler: qdbus6 nicht gefunden."
        out, _ = self._cmd([qd])
        players = [l.strip() for l in out.splitlines() if l.strip().startswith("org.mpris.MediaPlayer2.")]
        if not players:
            return "Kein Mediaplayer aktiv."
        path, iface = "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player"

        def prop(svc, name):
            return self._cmd([qd, svc, path, "org.freedesktop.DBus.Properties.Get", iface, name])[0]

        # bevorzugt den Player, der gerade spielt
        svc = next((p for p in players if prop(p, "PlaybackStatus") == "Playing"), players[0])
        method = {"play-pause": "PlayPause", "next": "Next", "previous": "Previous", "stop": "Stop",
                  "play": "Play", "pause": "Pause"}.get(what)
        if method:
            self._cmd([qd, svc, path, f"{iface}.{method}"])
        meta = prop(svc, "Metadata")
        title = re.search(r"xesam:title: (.*)", meta)
        artist = re.search(r"xesam:artist: (.*)", meta)
        status = prop(svc, "PlaybackStatus")
        name = svc.replace("org.mpris.MediaPlayer2.", "")
        song = " – ".join(x.group(1).strip() for x in (artist, title) if x)
        return f"{name}: {status}" + (f" · {song}" if song else "")

    # --- Erinnerungen / geplante Aufgaben ---
    def t_schedule_add(self, when, text, kind="erinnerung", repeat="", confirm=None):
        from . import scheduler
        t = scheduler.add(when, text, kind, repeat)
        what = "Erinnerung" if t["kind"] == "erinnerung" else "Aufgabe"
        return f"{what} geplant: {scheduler.describe(t)}"

    def t_schedule_list(self, confirm=None):
        from . import scheduler
        tasks = sorted(scheduler.load(), key=lambda t: t["next"])
        return "\n".join(scheduler.describe(t) for t in tasks) or "Nichts geplant."

    def t_schedule_delete(self, id, confirm=None):
        from . import scheduler
        return "Gelöscht." if scheduler.delete(id) else f"Keine Aufgabe mit id {id}."

    # --- Gedächtnis ---
    def t_memory_save(self, fact, confirm=None):
        if not self.settings.get("memory_enabled", True):
            return "Gedächtnis ist in den Einstellungen ausgeschaltet."
        m = memory.add(fact)
        return f"Gemerkt [{m['id']}]: {m['text']}"

    def t_memory_search(self, query="", confirm=None):
        hits = memory.search(query)
        if not hits:
            return "Nichts gefunden."
        return clip("\n".join(f"- [{m['id']}] {m['text']}" for m in hits), self.limit)

    def t_memory_delete(self, id, confirm=None):
        return "Gelöscht." if memory.delete(id) else f"Kein Eintrag mit id {id}."


def expand(names):
    """"*" steht für alle Werkzeuge inkl. Plugins (auch später hinzugefügte)."""
    if "*" in (names or []):
        return list(TOOL_SPECS) + list(plugins.PLUGINS)
    return list(names or [])


def specs_for(names):
    out = []
    for n in expand(names):
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
