"""Werkzeuge für den Agenten. Ausgaben werden hart begrenzt, um Kontext zu sparen."""
import html
import json
import re
import shlex
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

from . import jobs, mcp, memory, plugins, remote

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
                     "„cd“ bleibt für folgende Befehle erhalten. Für alles, was länger als ~1 Minute dauert "
                     "(Builds, Downloads, Tests, Kopieren), stattdessen job_start.",
                     {"command": {"type": "string"}}, ["command"]),
    "job_start": _fn("job_start", "Startet einen bash-Befehl als Hintergrundauftrag (Nutzer bestätigt) und kehrt "
                     "sofort zurück. Für lange Läufe: Builds, Downloads, Tests, große Kopien. Die Ausgabe geht in "
                     "eine Logdatei; wenn der Auftrag endet, bekommst du automatisch eine Nachricht mit "
                     "Rückgabewert und Log-Ende. Danach also NICHT in einer Schleife nachfragen, sondern andere "
                     "Arbeit erledigen oder die Antwort beenden. Startet im aktuellen Arbeitsordner.",
                     {"command": {"type": "string"},
                      "name": {"type": "string", "description": "kurzer Name, z. B. „Kernel bauen“"},
                      "host": {"type": "string", "description": "optional: Name eines SSH-Rechners – dann läuft der "
                               "Auftrag dort (im Ordner aus remote_shell)"}}, ["command"]),
    "remote_shell": _fn("remote_shell", "Führt einen bash-Befehl auf einem anderen Rechner per SSH aus (Liste der "
                        "Rechner im System-Prompt). „cd“ bleibt pro Rechner erhalten. sudo geht (ohne Passwort oder "
                        "spike fragt den Nutzer einmal). Kein interaktives Programm. Länger als ~1 Minute → "
                        "job_start mit host.",
                        {"host": {"type": "string", "description": "Name des Rechners"},
                         "command": {"type": "string"}}, ["host", "command"]),
    "watch_add": _fn("watch_add", "Richtet einen Wachposten ein: ein kurzer Prüf-Befehl läuft alle paar Minuten "
                     "(lokal oder mit host auf einem SSH-Rechner, ohne sudo-Passwort); wenn die Bedingung NEU eintritt, "
                     "wird der Nutzer benachrichtigt (action melden) bzw. du bekommst eine Nachricht und reagierst "
                     "(action reagieren, nur wenn der Modell-Server läuft). Der Befehl soll schnell sein und wenig "
                     "ausgeben (grep -c, tail -n, df --output=pcent, curl -fsS …). Bedingungen: enthaelt/fehlt "
                     "(value = Regex), fehler/ok (Rückgabewert), ueber/unter (value = Zahl, erste Zahl der Ausgabe), "
                     "aenderung (Ausgabe anders als zuletzt). Für Logs nur das Neue prüfen, z. B. "
                     "„docker logs --since 6m frigate 2>&1 | grep -ci error“ mit ueber 0 bei interval 5.",
                     {"name": {"type": "string", "description": "kurzer Name, z. B. „Platte zuhause“"},
                      "command": {"type": "string"},
                      "cond": {"type": "string", "enum": ["enthaelt", "fehlt", "fehler", "ok", "ueber", "unter", "aenderung"]},
                      "value": {"type": "string", "description": "Regex oder Zahl (je nach cond)"},
                      "host": {"type": "string", "description": "optional: SSH-Rechner"},
                      "interval": {"type": "integer", "description": "Minuten zwischen den Prüfungen (Standard 5)"},
                      "action": {"type": "string", "enum": ["melden", "reagieren"]},
                      "task": {"type": "string", "description": "bei reagieren: was du dann tun sollst"},
                      "may_act": {"type": "boolean", "description": "bei reagieren: darfst du dann ohne den Nutzer "
                                  "Befehle ausführen (nur im Freigabe-Modus Yolo wirksam)? Nur wenn er es ausdrücklich will."}},
                     ["name", "command", "cond"]),
    "watch_list": _fn("watch_list", "Zeigt alle Wachposten mit Zustand und letzter Ausgabe."),
    "watch_delete": _fn("watch_delete", "Löscht einen Wachposten.", {"id": {"type": "string"}}, ["id"]),
    "watch_toggle": _fn("watch_toggle", "Schaltet einen Wachposten an oder aus (ohne id: alle – Hauptschalter).",
                        {"id": {"type": "string"}, "on": {"type": "boolean"}}, ["on"]),
    "remote_copy": _fn("remote_copy", "Kopiert Dateien/Ordner zwischen diesem PC und einem SSH-Rechner (scp).",
                       {"host": {"type": "string"},
                        "richtung": {"type": "string", "enum": ["hoch", "runter"],
                                     "description": "hoch = von hier zum Rechner, runter = vom Rechner hierher"},
                        "von": {"type": "string", "description": "Quellpfad"},
                        "nach": {"type": "string", "description": "Zielpfad"}},
                       ["host", "richtung", "von", "nach"]),
    "job_status": _fn("job_status", "Zeigt Hintergrundaufträge: ohne id eine Liste (laufend und zuletzt beendet), "
                      "mit id Zustand und die letzten Zeilen des Logs. grep filtert das Log nach einem Muster "
                      "(z. B. „error|fehler“), lines = Anzahl Zeilen (Standard 40).",
                      {"id": {"type": "string"}, "lines": {"type": "integer"}, "grep": {"type": "string"}}),
    "job_stop": _fn("job_stop", "Bricht einen laufenden Hintergrundauftrag ab.", {"id": {"type": "string"}}, ["id"]),
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
    "pc": _fn("pc", "Steuert den Linux-PC (KDE). Aktionen: open (value = Datei/Ordner/URL öffnen ODER Programm "
              "starten per Name, z. B. chromium, dolphin, kate; mit program = Datei/URL in genau diesem Programm "
              "öffnen, z. B. value=https://youtube.com program=chromium), clipboard_get, clipboard_set (value=Text), notify (value=Text), volume (value=leer zum Abfragen, "
              "50 / +5 / -5 / mute), media (value=play-pause|next|previous|stop|status).",
              {"action": {"type": "string", "enum": ["open", "clipboard_get", "clipboard_set", "notify",
                                                     "volume", "media"]},
               "value": {"type": "string"},
               "program": {"type": "string", "description": "nur bei open: Programm, mit dem value geöffnet wird"}},
              ["action"]),
    "memory_save": _fn("memory_save", "Merkt sich dauerhaft einen kurzen Eintrag (ein Satz). Nur Wichtiges, das "
                       "später nützt; nichts Vertrauliches.",
                       {"fact": {"type": "string"},
                        "kind": {"type": "string", "enum": ["user", "note"],
                                 "description": "user = Fakt über den Nutzer (immer bekannt); note = Arbeitsnotiz zu "
                                                "einer Aufgabe/einem Ordner (nur bei Bedarf per memory_search)"}},
                       ["fact"]),
    "memory_search": _fn("memory_search", "Durchsucht das Gedächtnis nach Stichworten (leer = neueste).",
                         {"query": {"type": "string"}}),
    "memory_delete": _fn("memory_delete", "Löscht einen Gedächtnis-Eintrag per id (z. B. wenn er falsch ist).",
                         {"id": {"type": "string"}}, ["id"]),
    "ask_user": _fn("ask_user", "Stellt dem Nutzer eine Frage mit Antwort-Knöpfen und wartet auf seine Wahl. Nur nutzen, "
                    "wenn du wirklich eine Entscheidung brauchst (mehrere sinnvolle Wege, fehlende Angabe), nicht für "
                    "Rückfragen, die du selbst klären kannst. Der Nutzer kann auch frei antworten.",
                    {"question": {"type": "string", "description": "kurze, klare Frage"},
                     "options": {"type": "array", "items": {"type": "string"},
                                 "description": "2–6 kurze Antwortmöglichkeiten"},
                     "multi": {"type": "boolean", "description": "true = mehrere ankreuzbar, Standard false"}},
                    ["question", "options"]),
    "git": _fn("git", "Git: Änderungen ansehen, committen und pushen (wie ein Entwickler). Aktionen: status, diff, "
               "log, remotes (nur lesen, ohne Rückfrage); commit (message; files optional, sonst alle Änderungen – "
               "prüft automatisch auf Passwörter/Schlüssel), push (remote optional, Standard „lokal“ falls vorhanden, "
               "sonst „origin“), pull, init, remote_add (name + url), clone (url, path = Zielordner). "
               "Schreibende Aktionen fragt der Nutzer ab.",
               {"action": {"type": "string", "enum": ["status", "diff", "log", "remotes", "commit", "push", "pull",
                                                      "init", "remote_add", "clone"]},
                "path": {"type": "string", "description": "Repo-Ordner, Standard: aktueller Ordner"},
                "message": {"type": "string", "description": "nur commit: Commit-Nachricht"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "nur commit: nur diese Dateien (sonst alle Änderungen)"},
                "remote": {"type": "string", "description": "push/pull: Name des Remotes"},
                "name": {"type": "string", "description": "remote_add: Name, z. B. lokal"},
                "url": {"type": "string", "description": "remote_add/clone: Adresse des Repos"}},
               ["action"]),
    "unteragent": _fn("unteragent", "Startet einen Unteragenten, der eine abgegrenzte Teilaufgabe selbstständig erledigt "
                      "(eigener Chat, gleiche Werkzeuge, kann den Nutzer nicht fragen) und dir nur seinen Bericht "
                      "zurückgibt. Mehrere unteragent-Aufrufe in EINER Antwort laufen gleichzeitig. Beschreibe die "
                      "Aufgabe vollständig – der Unteragent sieht diesen Chat nicht.",
                      {"aufgabe": {"type": "string", "description": "vollständige Aufgabe: Ziel, Pfade, gewünschtes Ergebnis"},
                       "name": {"type": "string", "description": "Kurzname (2–4 Wörter) für die Anzeige"},
                       "kontext": {"type": "string", "description": "optional: nötiges Hintergrundwissen aus diesem Chat"}},
                      ["aufgabe"]),
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
    "ask_user": "Rückfrage mit Auswahl", "git": "Git: committen & pushen ⚠",
    "job_start": "Hintergrundauftrag starten ⚠", "job_status": "Hintergrundaufträge: Stand & Log",
    "job_stop": "Hintergrundauftrag abbrechen",
    "watch_add": "Wachposten einrichten ⚠", "watch_list": "Wachposten anzeigen", "watch_delete": "Wachposten löschen",
    "watch_toggle": "Wachposten an/aus",
    "remote_shell": "Andere Rechner: Befehle per SSH ⚠", "remote_copy": "Andere Rechner: Dateien kopieren ⚠",
    "unteragent": "Unteragenten starten (parallel)",
}

# Typische Geheimnisse, die nie in ein Repo gehören (geprüft werden nur hinzugefügte Zeilen)
SECRET_PATTERNS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "privater Schlüssel"),
    (r"\bghp_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}", "GitHub-Token"),
    (r"\bsk-[A-Za-z0-9_-]{20,}", "API-Schlüssel (sk-…)"),
    (r"\bxox[abpr]-[A-Za-z0-9-]{10,}", "Slack-Token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS-Schlüssel"),
    (r"(?i)\b(password|passwort|passwd|secret|token|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']",
     "Passwort/Token im Klartext"),
]
SECRET_FILES = re.compile(r"(^|/)(\.env(\..*)?|id_(rsa|ed25519|ecdsa)|.*\.(pem|key|p12|pfx)|credentials(\.json)?|"
                          r"git-credentials|.*token)$", re.I)


# ---------- Programme finden (.desktop-Starter und Befehle im PATH) ----------

def _app_dirs():
    home = Path.home()
    dirs = [Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))]
    dirs += [Path(d) for d in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if d]
    dirs += [home / ".local/share/flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    seen, out = set(), []
    for d in dirs:
        a = d / "applications"
        if a not in seen and a.is_dir():
            seen.add(a)
            out.append(a)
    return out


def _desktop_entries():
    entries = {}
    for base in _app_dirs():
        for f in base.rglob("*.desktop"):
            did = str(f.relative_to(base)).replace("/", "-")[:-len(".desktop")]
            if did in entries:          # erste Fundstelle gewinnt (wie beim Desktop)
                continue
            info = {"id": did, "path": str(f), "name": "", "generic": "", "exec": "", "hidden": False,
                    "names": [], "words": []}
            try:
                in_main = False
                for line in f.read_text("utf-8", "replace").splitlines():
                    line = line.strip()
                    if line.startswith("["):
                        in_main = line == "[Desktop Entry]"
                        continue
                    if not in_main or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    base_k = k.split("[", 1)[0]
                    if k == "Name":
                        info["name"] = v
                    if base_k == "Name":                 # auch übersetzte Namen (Name[de]=Systemeinstellungen)
                        info["names"].append(v.lower())
                    elif base_k in ("GenericName", "Keywords"):
                        info["words"] += [w.strip().lower() for w in v.split(";") if w.strip()]
                    if k == "GenericName":
                        info["generic"] = v
                    elif k == "Exec":
                        info["exec"] = v
                    elif k in ("NoDisplay", "Hidden") and v.lower() == "true":
                        info["hidden"] = True
                    elif k == "Type" and v != "Application":
                        info["hidden"] = True
            except OSError:
                continue
            entries[did] = info
    return list(entries.values())


def _exec_name(e):
    w = (e.get("exec") or "").split()
    w = [x for x in w if x != "env" and "=" not in x]
    return Path(w[0]).name.lower() if w else ""


def find_app(name):
    """Programm per Name finden: .desktop-Starter (Dateiname, Name, Startbefehl), sonst Befehl im PATH.
    Gibt {"id", "name", "exe"} oder None zurück."""
    q = name.strip().lower().removesuffix(".desktop")
    if not q:
        return None
    entries = [e for e in _desktop_entries() if e["exec"]]
    ranks = (
        lambda e: e["id"].lower() == q,
        lambda e: e["id"].lower().endswith("." + q),
        lambda e: q in e["names"],
        lambda e: _exec_name(e) == q,
        lambda e: not e["hidden"] and any(q in n for n in e["names"]),
        lambda e: not e["hidden"] and q in e["words"],
        lambda e: not e["hidden"] and any(q in w for w in e["words"]),
    )
    for test in ranks:
        hits = [e for e in entries if test(e)]
        if hits:
            hits.sort(key=lambda e: (e["hidden"], len(e["name"])))
            e = hits[0]
            return {"id": e["id"], "name": e["name"] or e["id"], "exe": shutil.which(_exec_name(e)) or ""}
    exe = shutil.which(name.strip())
    return {"id": "", "name": Path(exe).name, "exe": exe} if exe else None


def similar_apps(name, limit=6):
    q = name.strip().lower()[:4]
    names = sorted({e["name"] for e in _desktop_entries() if not e["hidden"] and e["name"]
                    and q and (any(q in n for n in e["names"]) or q in e["id"].lower())})
    return names[:limit]


# ---------- Rückgängig: Sicherung vor jeder Dateiänderung ----------

def _backup_dir():
    from .config import DATA_DIR
    return DATA_DIR / "backups"


def backup_file(path: Path, new_content: str) -> dict:
    """Sichert den alten Stand (oder merkt sich, dass die Datei neu ist). Gibt Infos fürs Rückgängigmachen."""
    import hashlib
    import time as _t
    import uuid as _u
    info = {"path": str(path), "existed": path.exists(), "backup": "",
            "new_hash": hashlib.sha256(new_content.encode("utf-8")).hexdigest(), "time": _t.time()}
    if path.exists():
        d = _backup_dir()
        d.mkdir(parents=True, exist_ok=True)
        b = d / f"{_t.strftime('%Y%m%d-%H%M%S')}-{_u.uuid4().hex[:6]}-{path.name}"
        shutil.copy2(path, b)
        info["backup"] = str(b)
    return info


def undo_change(info: dict, force=False) -> str:
    """Stellt den Stand vor der Änderung wieder her. Wirft ValueError, wenn die Datei seitdem verändert wurde."""
    import hashlib
    p = Path(info["path"])
    if p.exists() and not force:
        cur = hashlib.sha256(p.read_bytes()).hexdigest()
        if cur != info.get("new_hash"):
            raise ValueError(f"{p.name} wurde seit der Änderung weiter verändert")
    if info.get("existed"):
        shutil.copy2(info["backup"], p)
        return f"{p} wiederhergestellt"
    p.unlink(missing_ok=True)
    return f"{p} gelöscht (war neu angelegt)"


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
# Pro Chat wird ein einmal eingegebenes, geprüftes Passwort im Arbeitsspeicher gehalten (nie auf Platte,
# nie im Chat). Für einen Befehl liegt es kurz in einer 0600-Datei in XDG_RUNTIME_DIR; askpass.sh reicht es
# einmal pro sudo-Aufruf weiter – ist es falsch, fragt sudo erneut und askpass zeigt wieder das Fenster.

_SUDO_PW: dict[str, str] = {}      # "session" → Passwort (nur RAM, gilt bis spike beendet wird)
_SUDO_KEY = "session"              # einmal eingeben, dann für alle Chats/Aufträge dieser Sitzung


def forget_sudo(key=None):
    if key is None:
        _SUDO_PW.clear()
    else:
        _SUDO_PW.pop(key, None)


def _sudo_prepare(askpass: str):
    """Legt das gemerkte Passwort für einen Befehl/Auftrag bereit.
    Gibt (env-Zusätze, pw, done) zurück; done() räumt auf und merkt ein neu eingegebenes, richtiges Passwort."""
    if not askpass:
        return {}, "", lambda: None
    pw = _SUDO_PW.get(_SUDO_KEY, "")
    pw_dir = Path(tempfile.mkdtemp(prefix="spike-sudo-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
    pw_file, pw_save = pw_dir / "pw", pw_dir / "neu"
    if pw:
        fd = os.open(pw_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(pw + "\n")

    def done():
        try:
            new = pw_save.read_text() if pw_save.exists() else ""
        except OSError:
            new = ""
        shutil.rmtree(pw_dir, ignore_errors=True)
        if new and new != _SUDO_PW.get(_SUDO_KEY) and _sudo_ok(new):      # nur ein richtiges Passwort merken
            _SUDO_PW[_SUDO_KEY] = new
    env = {"SUDO_ASKPASS": askpass, "SPIKE_PW_FILE": str(pw_file), "SPIKE_PW_SAVE": str(pw_save),
           "SPIKE_PW_DIR": str(pw_dir)}
    return env, pw, done


def _sudo_ok(pw: str) -> bool:
    """Prüft das Passwort mit sudo -S (ohne Fenster)."""
    try:
        r = subprocess.run(["sudo", "-S", "-k", "-v", "-p", ""], input=pw + "\n", text=True,
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _askpass() -> str:
    tool = shutil.which("kdialog") or shutil.which("zenity")
    if not tool:
        return ""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / "askpass.sh"
    if "kdialog" in tool:
        ask = 'kdialog --title "spike – Administrator-Rechte" --password "${1:-Passwort für sudo:}"'
    else:
        ask = 'zenity --password --title "spike – Administrator-Rechte"'
    text = ("#!/bin/sh\n# von spike erzeugt: fragt das sudo-Passwort grafisch ab\n"
            "# gemerktes Passwort des Chats: einmal pro sudo-Aufruf ($PPID), danach wieder das Fenster\n"
            'if [ -n "$SPIKE_PW_FILE" ] && [ -s "$SPIKE_PW_FILE" ] && [ ! -e "$SPIKE_PW_FILE.$PPID" ]; then\n'
            '  : > "$SPIKE_PW_FILE.$PPID"; cat "$SPIKE_PW_FILE"; exit 0\nfi\n'
            f"pw=$({ask}) || exit 1\n"
            'if [ -n "$SPIKE_PW_SAVE" ]; then (umask 077; printf \'%s\' "$pw" > "$SPIKE_PW_SAVE"); fi\n'
            '# gleich für weitere sudo-Aufrufe im selben Befehl bereitlegen\n'
            'if [ -n "$SPIKE_PW_FILE" ]; then (umask 077; printf \'%s\\n\' "$pw" > "$SPIKE_PW_FILE"; '
            ': > "$SPIKE_PW_FILE.$PPID"); fi\n'
            "printf '%s\\n' \"$pw\"\n")
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
        self.last_undo = None         # Sicherung der letzten Dateiänderung (für Rückgängig)
        self.vision = False           # versteht das Modell Bilder?
        self._images = []             # Bilder aus Werkzeugen, die als nächste Nachricht ans Modell gehen
        self._proc = None             # laufender Shell-Prozess (für Stopp)
        self.sudo_key = None          # Chat-ID (für Aufträge); sudo-Passwort gilt für die ganze Sitzung
        self.remote_cwd = {}          # SSH-Rechner → zuletzt benutzter Ordner dort

    def _mcp_image(self, b64, mime):
        import base64
        from . import attachments
        attachments.ATT_DIR.mkdir(parents=True, exist_ok=True)
        ext = ".png" if "png" in mime else ".jpg"
        f = attachments.ATT_DIR / f"mcp-{len(self._images)}-{os.getpid()}-{__import__('time').time():.0f}{ext}"
        f.write_bytes(base64.b64decode(b64))
        if self.vision:
            self._images.append({"name": f.name, "path": str(f), "orig": "", "kind": "image", "size": f.stat().st_size})

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
        sudo_env, pw, sudo_done = _sudo_prepare(askpass)
        env = {**os.environ, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat",
               "NO_COLOR": "1", **sudo_env}

        def hide(line):         # Passwort taucht nie in Ausgabe/Chat auf
            return line.replace(pw, "•••") if pw and len(pw) >= 3 else line
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
                line = hide(line)
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
        sudo_done()
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
            if name in mcp.TOOLS:
                return clip_tail(mcp.call(name, args, confirm, self.settings.get("mcp_servers"),
                                          on_image=self._mcp_image), self.limit)
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

    # --- Andere Rechner per SSH ---
    def _run_argv(self, argv, stdin_data="", timeout=None, on_output=None, hide_pw=""):
        """Startet argv mit Live-Ausgabe (wie shell). Gibt (exit, text, ordner_vom_rechner) zurück."""
        import signal
        import threading as _th
        timeout = timeout or int(self.settings.get("shell_timeout", 300))
        env = {**os.environ, "LC_ALL": "C.UTF-8"}
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1, start_new_session=True, env=env)
        self._proc = p
        out, cwd = [], [None]

        def reader():
            for line in p.stdout:
                i = line.find(remote.CWD_MARK)
                if i >= 0:
                    cwd[0] = line[i + len(remote.CWD_MARK):].strip()
                    line = line[:i]
                    if not line:
                        continue
                if hide_pw and len(hide_pw) >= 3:
                    line = line.replace(hide_pw, "•••")
                out.append(line)
                if on_output:
                    on_output(line)
        t = _th.Thread(target=reader, daemon=True)
        t.start()
        try:
            p.stdin.write(stdin_data)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        t.join(timeout=2)
        self._proc = None
        text = "".join(out).rstrip("\n")
        if timed_out:
            text += f"\n[abgebrochen nach {timeout} s]"
        return p.returncode, text, cwd[0]

    def _host(self, host):
        h = remote.find(self.settings, host)
        if h:
            return h, ""
        names = ", ".join(x["name"] for x in remote.hosts(self.settings)) or "keine eingerichtet"
        return None, (f"Fehler: Rechner „{host}“ ist nicht eingerichtet (bekannt: {names}). Der Nutzer kann Rechner "
                      f"unter ⚙ Einstellungen → Rechner (SSH) hinzufügen.")

    def t_remote_shell(self, host, command, confirm=None):
        h, err = self._host(host)
        if err:
            return err
        name = h["name"]
        if h.get("mode") == "lesen":
            if not remote.is_readonly(command):
                return (f"Gesperrt: „{name}“ ist nur zum Lesen freigegeben – dieser Befehl könnte etwas ändern. "
                        f"Beschreibe dem Nutzer, was du tun würdest; er kann den Rechner in den Einstellungen auf "
                        f"„voll“ stellen.")
        elif not confirm(f"Auf „{name}“ ({h['target']}) ausführen?\n\n{command}"):
            return "Vom Nutzer abgelehnt."
        cwd = self.remote_cwd.get(name, "")
        argv = remote.remote_argv(h["target"], command, cwd)
        pw = remote.password(name)
        rc, out, new_cwd = self._run_argv(argv, pw + "\n", on_output=self.on_output, hide_pw=pw)
        # sudo braucht ein Passwort → einmal fragen (für diesen Rechner und diese Sitzung) und wiederholen
        if rc != 0 and not pw and remote.needs_password(out) and remote.ask_password(h, _askpass()):
            pw = remote.password(name)
            if self.on_output:
                self.on_output("\n[sudo-Passwort gemerkt – Befehl wird wiederholt]\n")
            rc, out, new_cwd = self._run_argv(argv, pw + "\n", on_output=self.on_output, hide_pw=pw)
        if new_cwd:
            self.remote_cwd[name] = new_cwd
        if rc == 255 and not new_cwd:
            out += ("\n[SSH-Verbindung fehlgeschlagen – Rechner aus, Netz weg oder Schlüssel fehlt "
                    f"(einmal im Terminal: ssh-copy-id {h['target'].split(':')[0]})]")
        return f"exit={rc} · Ordner: {name}:{self.remote_cwd.get(name, '~')}\n" + clip_tail(out, self.limit)

    def t_remote_copy(self, host, richtung, von, nach, confirm=None):
        h, err = self._host(host)
        if err:
            return err
        up = str(richtung).lower().startswith("h")
        if h.get("mode") == "lesen" and up:
            return f"Gesperrt: „{h['name']}“ ist nur zum Lesen freigegeben – hochladen geht nicht, runterladen schon."
        local = Path(von if up else nach).expanduser()
        local = str(local if local.is_absolute() else Path(self.cwd) / local)
        rpath = nach if up else von
        cwd = self.remote_cwd.get(h["name"], "")
        if cwd and not rpath.startswith(("/", "~")):
            rpath = f"{cwd}/{rpath}"
        what = (f"{local} → {h['name']}:{rpath}" if up else f"{h['name']}:{rpath} → {local}")
        if h.get("mode") != "lesen" and not confirm(f"Kopieren ({'hoch' if up else 'runter'})?\n\n{what}"):
            return "Vom Nutzer abgelehnt."
        if not up:
            Path(local).parent.mkdir(parents=True, exist_ok=True)
        argv = remote.scp_argv(h["target"], local if up else rpath, rpath if up else local, up)
        rc, out, _ = self._run_argv(argv, "", on_output=self.on_output)
        return (f"Kopiert: {what}" if rc == 0 else f"Fehler beim Kopieren (exit {rc}): {what}\n{clip_tail(out, 1500)}")

    # --- Wachposten ---
    def t_watch_add(self, name, command, cond, value="", host="", interval=5, action="melden", task="",
                    may_act=False, confirm=None):
        from . import watch as W
        if host:
            h, err = self._host(host)
            if err:
                return err
            host = h["name"]
            if h.get("mode") == "lesen" and not remote.is_readonly(command):
                return f"Gesperrt: „{host}“ ist nur zum Lesen freigegeben – der Prüf-Befehl darf nichts ändern."
        where = f" auf {host}" if host else ""
        val = f" „{value}“" if value else ""
        text = (f"Wachposten „{name}“ einrichten?\n\nAlle {interval} min{where}:\n{command}\n\nWenn: "
                f"{W.CONDS.get(cond, cond)}{val} → {action}" + (f"\nDann: {task}" if task else "")
                + ("\n⚠ darf dann selbst handeln" if may_act else ""))
        if not confirm(text):
            return "Vom Nutzer abgelehnt."
        try:
            w = W.add(name, command, cond, value, host, interval, action, task, self.sudo_key or "", may_act)
        except Exception as e:
            return f"Fehler: {e}"
        rc, out = W.run_check(w, self.settings)          # einmal probelaufen lassen
        hit = W.evaluate(w, rc, out)
        aus = "" if self.settings.get("watch_enabled", True) else (
            "\nHinweis: Der Hauptschalter für Wachposten ist AUS – der Nutzer schaltet ihn über 👁 unter dem "
            "Eingabefeld ein.")
        return (f"Wachposten [{w['id']}] eingerichtet. Probelauf: exit={rc}, Bedingung jetzt "
                f"{'ERFÜLLT (meldet sich gleich)' if hit else 'nicht erfüllt' if hit is False else 'nicht prüfbar'}."
                f"\nAusgabe: {clip_tail(out, 600) or '(leer)'}{aus}")

    def t_watch_list(self, confirm=None):
        from . import watch as W
        if not W.WATCHES:
            return "Keine Wachposten."
        head = "Hauptschalter: " + ("an" if self.settings.get("watch_enabled", True) else "AUS") + "\n"
        rows = []
        for w in W.WATCHES.values():
            last = (w.get("last_out") or "").strip().splitlines()[-1:] or [""]
            rows.append(W.describe(w) + (f"\n   Befehl: {w['command'][:150]}") + (f"\n   zuletzt: {last[0][:150]}" if last[0] else "")
                        + (f"\n   Fehler: {w['error']}" if w.get("error") else ""))
        return head + "\n".join(rows)

    def t_watch_delete(self, id, confirm=None):
        from . import watch as W
        return f"Wachposten {id} gelöscht." if W.remove(str(id)) else f"Kein Wachposten {id}."

    def t_watch_toggle(self, on, id="", confirm=None):
        from . import watch as W
        if not id:
            self.settings["watch_enabled"] = bool(on)
            from . import config as C
            cfg = C.load_config()
            cfg["settings"]["watch_enabled"] = bool(on)
            C.save_config(cfg)
            W._changed()
            return "Hauptschalter Wachposten: " + ("an" if on else "aus")
        if str(id) not in W.WATCHES:
            return f"Kein Wachposten {id}."
        W.set_enabled(str(id), bool(on))
        return f"Wachposten {id}: " + ("an" if on else "aus")

    # --- Hintergrundaufträge (laufen abgekoppelt weiter, Rückmeldung kommt in den Chat) ---
    def t_job_start(self, command, name="", host="", confirm=None):
        if host:
            return self._remote_job(host, command, name, confirm)
        if not confirm(f"Hintergrundauftrag starten?\n\n{command}"):
            return "Vom Nutzer abgelehnt."
        askpass = _askpass()
        cwd = self.cwd if Path(self.cwd).is_dir() else Path.home()
        sudo_env, _pw, sudo_done = _sudo_prepare(askpass)
        # Passwort-Datei löscht auch bash selbst am Ende (falls spike vorher neu startet)
        prelude = ('sudo() { command sudo -A "$@"; }\ntrap \'rm -f "$SPIKE_PW_FILE"\' EXIT\n'
                   if askpass else "")
        job = jobs.start(command, cwd, chat_id=self.sudo_key or "", name=name,
                         env=sudo_env or None, prelude=prelude, on_end=sudo_done)
        return (f"Auftrag [{job['id']}] „{job['name']}“ gestartet (PID {job['pid']}, Ordner {cwd}).\n"
                f"Log: {job['log']}\nDu bekommst automatisch eine Nachricht, wenn er fertig ist – "
                f"nicht in einer Schleife nachfragen. Zwischenstand bei Bedarf mit job_status(id=\"{job['id']}\").")

    def _remote_job(self, host, command, name, confirm):
        h, err = self._host(host)
        if err:
            return err
        if h.get("mode") == "lesen" and not remote.is_readonly(command):
            return f"Gesperrt: „{h['name']}“ ist nur zum Lesen freigegeben."
        if h.get("mode") != "lesen" and not confirm(f"Hintergrundauftrag auf „{h['name']}“ starten?\n\n{command}"):
            return "Vom Nutzer abgelehnt."
        # sudo ohne gemerktes Passwort: vorab prüfen, ob es eins braucht, und dann einmal fragen
        if "sudo" in command and not remote.password(h["name"]):
            rc, out, _ = self._run_argv(remote.remote_argv(h["target"], "sudo -n true", mark=False), "\n", timeout=20)
            if rc != 0 and remote.needs_password(out):
                remote.ask_password(h, _askpass())
        pw = remote.password(h["name"])
        argv = remote.remote_argv(h["target"], command, self.remote_cwd.get(h["name"], ""), persist=False, mark=False)
        pw_dir = Path(tempfile.mkdtemp(prefix="spike-ssh-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
        pw_file = pw_dir / "in"
        fd = os.open(pw_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(pw + "\n")
        # Passwort-Datei nur so lange, bis ssh sie gelesen hat
        local = f"trap 'rm -rf {shlex.quote(str(pw_dir))}' EXIT\n{' '.join(shlex.quote(a) for a in argv)} < {shlex.quote(str(pw_file))}"
        job = jobs.start(local, Path.home(), chat_id=self.sudo_key or "", name=name or f"{h['name']}: {command[:40]}",
                         on_end=lambda: shutil.rmtree(pw_dir, ignore_errors=True), shown=f"[{h['name']}] {command}")
        return (f"Auftrag [{job['id']}] „{job['name']}“ läuft auf {h['name']} (lokale PID {job['pid']}).\n"
                f"Log: {job['log']}\nDu bekommst automatisch eine Nachricht, wenn er fertig ist – nicht in einer "
                f"Schleife nachfragen.")

    def t_job_status(self, id="", lines=40, grep="", confirm=None):
        if not id:
            items = sorted(jobs.JOBS.values(), key=lambda j: (bool(j.get("end")), -j["start"]))[:12]
            if not items:
                return "Keine Hintergrundaufträge."
            return "\n".join(jobs.describe(j) for j in items)
        job = jobs.get(str(id))
        if not job:
            return f"Kein Auftrag mit id {id}. Liste mit job_status()."
        out = jobs.tail(job["id"], lines=lines, grep=grep)
        kopf = jobs.describe(job) + f"\nBefehl: {job['command'][:300]}\nLog: {job['log']}"
        return kopf + ("\n--- Log" + (f" (nur Zeilen mit „{grep}“)" if grep else "") + " ---\n" +
                       clip_tail(out, self.limit) if out else "\n(noch keine Ausgabe)")

    def t_job_stop(self, id, confirm=None):
        return jobs.stop(str(id))

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
        self.last_undo = backup_file(p, updated)
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
        self.last_undo = backup_file(p, content)
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

    def t_pc(self, action, value="", program="", confirm=None):
        value, program = (value or "").strip(), (program or "").strip()
        if action == "open":
            return self._open(value, program, confirm)
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

    # --- Öffnen: Datei/URL mit Standardprogramm, Programm per Name starten, oder Datei/URL mit Programm X ---
    def _open(self, value, program, confirm):
        if not value and not program:
            return "Fehler: value (Pfad, URL oder Programmname) fehlt."
        is_url = bool(re.match(r"^[a-z][a-z0-9+.-]*://", value)) or value.startswith(("mailto:", "www."))
        if value.startswith("www."):
            value = "https://" + value
        path = Path(value).expanduser() if value and not is_url else None
        exists = bool(path and path.exists())
        if not program and value and not is_url and not exists and "/" not in value:
            program, value = value, ""          # kein Pfad, keine URL → Programmname
        target = value if is_url or not value else str(path)
        if program:
            app = find_app(program)
            if not app:
                return (f"Fehler: Programm „{program}“ nicht gefunden (weder Starter noch Befehl). "
                        f"Ähnliche: {', '.join(similar_apps(program)) or '–'}")
            label = f"{app['name']}" + (f" mit {target}" if target else "")
            if not confirm(f"Programm starten?\n\n{label}"):
                return "Vom Nutzer abgelehnt."
            if app.get("id") and shutil.which("gtk-launch"):
                cmd = ["gtk-launch", app["id"]] + ([target] if target else [])
            else:
                cmd = [app["exe"]] + ([target] if target else [])
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return f"Gestartet: {label}"
        if not is_url and not exists:
            return f"Fehler: {target} gibt es nicht."
        if not confirm(f"Öffnen?\n\n{target}"):
            return "Vom Nutzer abgelehnt."
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return f"Geöffnet: {target}"

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
    def t_memory_save(self, fact, kind="note", confirm=None):
        if not self.settings.get("memory_enabled", True):
            return "Gedächtnis ist in den Einstellungen ausgeschaltet."
        m = memory.add(fact, kind=kind)
        what = "Über dich" if m.get("kind") == "user" else "Arbeitsnotiz"
        return f"Gemerkt als {what} [{m['id']}]: {m['text']}"

    def t_memory_search(self, query="", confirm=None):
        hits = memory.search(query)
        if not hits:
            return "Nichts gefunden."
        return clip("\n".join(f"- [{m['id']}]{'' if memory.is_always(m) else ' (Notiz)'} {m['text']}"
                              for m in hits), self.limit)

    def t_memory_delete(self, id, confirm=None):
        return "Gelöscht." if memory.delete(id) else f"Kein Eintrag mit id {id}."

    # --- Git ---
    def _git(self, repo, *args, timeout=60):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "NO_COLOR": "1",
               "LC_ALL": "C.UTF-8"}
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace",
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()

    @staticmethod
    def _secrets(diff: str) -> list:
        hits, cur = [], ""
        for line in diff.splitlines():
            if line.startswith("+++ "):
                cur = line[6:] if line.startswith("+++ b/") else line[4:]
                if SECRET_FILES.search(cur):
                    hits.append(f"{cur}: Datei sieht nach Zugangsdaten aus")
                continue
            if line.startswith("+") and not line.startswith("+++"):
                for pat, what in SECRET_PATTERNS:
                    if re.search(pat, line):
                        hits.append(f"{cur}: {what}: {line[1:].strip()[:60]}…")
                        break
        return hits[:10]

    def t_git(self, action, path="", message="", files=None, remote="", name="", url="", confirm=None):
        repo = self._path(path) if path else self.cwd
        if action == "clone":
            if not url:
                return "Fehler: clone braucht url."
            target = self._path(path) if path else self.cwd / Path(url.rstrip("/")).name.removesuffix(".git")
            if target.exists() and any(target.iterdir()):
                return f"Fehler: {target} existiert schon und ist nicht leer."
            if not confirm(f"Repo klonen?\n\n{url}\n→ {target}"):
                return "Vom Nutzer abgelehnt."
            rc, out = self._git(target.parent, "clone", url, str(target), timeout=300)
            return f"exit={rc}\n{clip_tail(out, self.limit)}"
        if not Path(repo).is_dir():
            return f"Fehler: Ordner {repo} gibt es nicht."
        if action == "init":
            if (Path(repo) / ".git").exists():
                return f"{repo} ist schon ein Git-Repo."
            if not confirm(f"Neues Git-Repo anlegen in\n{repo}?"):
                return "Vom Nutzer abgelehnt."
            rc, out = self._git(repo, "init", "-b", "main")
            return f"exit={rc}\n{out}"
        rc, top = self._git(repo, "rev-parse", "--show-toplevel")
        if rc:
            return f"Fehler: {repo} ist kein Git-Repo (erst action=init)."
        repo = Path(top)
        _, branch = self._git(repo, "branch", "--show-current")
        _, remotes = self._git(repo, "remote")
        remotes = remotes.split()
        has_head = self._git(repo, "rev-parse", "-q", "--verify", "HEAD")[0] == 0
        if action == "status":
            _, st = self._git(repo, "status", "-sb")
            last = self._git(repo, "log", "--oneline", "-3")[1] if has_head else "noch keine"
            return clip(f"Repo: {repo}\nRemotes: {', '.join(remotes) or 'keine'}\n{st}\n\nLetzte Commits:\n{last}",
                        self.limit)
        if action == "diff":
            base = ["HEAD"] if has_head else ["--cached"]     # ohne Commit: bereits Vorgemerktes zeigen
            _, stat = self._git(repo, "diff", *base, "--stat")
            _, d = self._git(repo, "diff", *base)
            _, new = self._git(repo, "ls-files", "--others", "--exclude-standard")
            extra = f"\n\nNeue (noch unbekannte) Dateien:\n{new}" if new else ""
            return clip(f"{stat}{extra}\n\n{d}" if (stat or new) else "Keine Änderungen.", self.limit)
        if action == "log":
            if not has_head:
                return "Noch keine Commits."
            return self._git(repo, "log", "--oneline", "--decorate", "-20")[1]
        if action == "remotes":
            _, out = self._git(repo, "remote", "-v")
            return out or "Keine Remotes eingetragen."
        if action == "remote_add":
            if not (name and url):
                return "Fehler: remote_add braucht name und url."
            if not confirm(f"Remote „{name}“ eintragen?\n\n{url}"):
                return "Vom Nutzer abgelehnt."
            rc, out = self._git(repo, "remote", "add", name, url)
            return f"exit={rc}\n{out}" if rc else f"Remote „{name}“ → {url} eingetragen."
        if action == "commit":
            if not message.strip():
                return "Fehler: commit braucht eine message."
            _, who = self._git(repo, "config", "user.email")
            if not who:
                return ("Fehler: Für dieses Repo ist kein Autor eingetragen (git config user.name / user.email). "
                        "Frag den Nutzer, welcher Name und welche E-Mail rein sollen, und setze sie mit run_shell "
                        "nur für dieses Repo.")
            _, staged_before = self._git(repo, "diff", "--cached", "--name-only")
            rc, out = self._git(repo, "add", "--", *files) if files else self._git(repo, "add", "-A")
            if rc:
                return f"Fehler beim Hinzufügen:\n{out}"
            _, stat = self._git(repo, "diff", "--cached", "--stat")
            if not stat:
                return "Nichts zu committen – keine Änderungen."
            _, d = self._git(repo, "diff", "--cached")
            undo = (lambda: None) if staged_before else (lambda: self._git(repo, "reset", "-q"))
            hits = self._secrets(d)
            if hits:
                undo()
                return ("NICHT committet – mögliche Geheimnisse gefunden:\n" + "\n".join(hits) +
                        "\n\nSag das dem Nutzer. Lösung: Datei in .gitignore eintragen oder den Wert entfernen.")
            if not confirm(f"Commit in {repo.name} ({branch}):\n\n{message}\n\n{stat}", diff=clip(d, 30000)):
                undo()
                return "Vom Nutzer abgelehnt."
            rc, out = self._git(repo, "commit", "-q", "-m", message)
            if rc:
                return f"Commit fehlgeschlagen:\n{out}"
            _, h = self._git(repo, "log", "--oneline", "-1")
            return f"Committet: {h}\n{stat.splitlines()[-1] if stat else ''}"
        if action in ("push", "pull"):
            remote = remote or ("lokal" if "lokal" in remotes else "origin" if "origin" in remotes else "")
            if remote not in remotes:
                return (f"Fehler: Remote „{remote or '?'}“ fehlt. Vorhanden: {', '.join(remotes) or 'keine'}. "
                        "Mit action=remote_add eintragen.")
            if not branch:
                return "Fehler: kein Branch ausgecheckt."
            _, rurl = self._git(repo, "remote", "get-url", remote)
            if action == "pull":
                if not confirm(f"Neueste Version holen?\n\n{rurl} ({branch}) → {repo}"):
                    return "Vom Nutzer abgelehnt."
                rc, out = self._git(repo, "pull", "--ff-only", remote, branch, timeout=180)
                return f"exit={rc}\n{clip_tail(out, self.limit)}"
            self._git(repo, "fetch", "-q", remote, timeout=60)
            rc, ahead = self._git(repo, "log", "--oneline", f"{remote}/{branch}..HEAD")
            if rc:   # Branch gibt es dort noch nicht → alles ist neu
                _, ahead = self._git(repo, "log", "--oneline", "-20")
            if not ahead:
                return f"Nichts zu pushen – {remote}/{branch} ist schon aktuell."
            n = len(ahead.splitlines())
            if not confirm(f"Pushen nach „{remote}“?\n{rurl} ({branch})\n\n{n} Commit(s):\n{ahead[:1500]}"):
                return "Vom Nutzer abgelehnt."
            rc, out = self._git(repo, "push", "-u", remote, branch, timeout=180)
            if rc:
                return f"Push fehlgeschlagen (exit={rc}):\n{clip_tail(out, self.limit)}"
            return f"Gepusht nach {remote} ({rurl}), Branch {branch}: {n} Commit(s).\n{clip_tail(out, 1500)}"
        return f"Fehler: unbekannte Aktion '{action}'."

    # --- Rückfrage (die eigentliche Karte zeigt der Agent, siehe Agent._ask) ---
    def t_ask_user(self, question, options=None, multi=False, confirm=None):
        return "Fehler: Rückfragen gehen nur im Chat."


def expand(names):
    """"*" steht für alle Werkzeuge inkl. Plugins (auch später hinzugefügte)."""
    if "*" in (names or []):
        return list(TOOL_SPECS) + list(plugins.PLUGINS) + list(mcp.TOOLS)
    return list(names or [])


def specs_for(names):
    out = []
    for n in expand(names):
        if n in TOOL_SPECS:
            out.append(TOOL_SPECS[n])
        elif n in plugins.PLUGINS:
            out.append(plugins.PLUGINS[n]["spec"])
        elif n in mcp.TOOLS:
            out.append(mcp.TOOLS[n]["spec"])
    return out


def all_tools():
    """[(name, anzeigename)] – eingebaute Werkzeuge und geladene Plugins."""
    return [(n, TOOL_LABELS.get(n, n)) for n in TOOL_SPECS] + \
           [(n, p["label"]) for n, p in plugins.PLUGINS.items()] + \
           [(n, t["label"]) for n, t in mcp.TOOLS.items()]


def load_plugins():
    return plugins.load(set(TOOL_SPECS))


def short_args(arguments: str, n=120) -> str:
    try:
        a = json.loads(arguments or "{}")
        s = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in a.items())
    except Exception:
        s = arguments or ""
    return s if len(s) <= n else s[:n] + "…"
