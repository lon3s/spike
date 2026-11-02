"""Plugin-System: jede .py-Datei in ~/.config/spike/tools/ kann Werkzeuge mit @tool registrieren.

Beispiel (~/.config/spike/tools/wetter.py):

    from spike.plugins import tool

    @tool("Aktuelles Wetter für eine Stadt.", params={"stadt": "Name der Stadt"})
    def wetter(stadt: str, ctx) -> str:
        return ctx.http_get(f"https://wttr.in/{stadt}?format=3&lang=de")

- Parameter und Typen kommen aus der Funktionssignatur (str, int, float, bool, list).
  Parameter ohne Standardwert sind Pflicht.
- `params` (optional) beschreibt die Parameter für das Modell.
- Heißt ein Parameter `ctx`, bekommt er Hilfsfunktionen: ctx.http_get, ctx.html_to_text,
  ctx.confirm("Frage?") -> bool, ctx.run(befehl) -> str, ctx.settings, ctx.limit.
- `confirm=True` fragt vor jedem Aufruf beim Nutzer nach.
- Die Rückgabe wird in Text umgewandelt und automatisch gekürzt.
- Dateien, die mit "_" beginnen, werden ignoriert.
"""
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from .config import CONFIG_DIR

PLUGIN_DIR = CONFIG_DIR / "tools"
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "plugins"

PLUGINS: dict[str, dict] = {}     # name -> {"func", "spec", "label", "confirm", "file"}
ERRORS: list[str] = []
_pending: list[dict] = []

_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def tool(description: str, params: dict | None = None, label: str | None = None,
         confirm: bool = False, name: str | None = None):
    """Dekorator, der eine Funktion als Werkzeug für das Modell registriert."""
    def deco(func):
        sig = inspect.signature(func)
        props, required = {}, []
        for pname, p in sig.parameters.items():
            if pname == "ctx":
                continue
            ann = p.annotation if p.annotation is not inspect.Parameter.empty else str
            info = (params or {}).get(pname)
            schema = dict(info) if isinstance(info, dict) else {"type": _TYPES.get(ann, "string")}
            if isinstance(info, str):
                schema["description"] = info
            if schema.get("type") == "array" and "items" not in schema:
                schema["items"] = {"type": "string"}
            props[pname] = schema
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        tname = name or func.__name__
        _pending.append({
            "name": tname,
            "func": func,
            "label": (label or tname) + (" ⚠" if confirm else "") + "  (Plugin)",
            "confirm": confirm,
            "wants_ctx": "ctx" in sig.parameters,
            "spec": {"type": "function", "function": {
                "name": tname, "description": description,
                "parameters": {"type": "object", "properties": props, "required": required}}},
        })
        return func
    return deco


class Ctx:
    """Hilfsfunktionen für Plugins."""

    def __init__(self, toolbox, confirm):
        from . import tools
        self.settings = toolbox.settings
        self.limit = toolbox.limit
        self.http_get = tools.http_get
        self.html_to_text = tools.html_to_text
        self.clip = tools.clip
        self.confirm = confirm

    def run(self, command: str, timeout: int = 60) -> str:
        p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=Path.home())
        return f"{p.stdout}{p.stderr}".strip() or f"(keine Ausgabe, exit={p.returncode})"


def ensure_dir():
    """Legt den Plugin-Ordner an; beim ersten Mal werden die Beispiele hineinkopiert."""
    if PLUGIN_DIR.exists():
        return
    PLUGIN_DIR.mkdir(parents=True)
    if EXAMPLES_DIR.is_dir():
        for f in EXAMPLES_DIR.glob("*.py"):
            shutil.copy(f, PLUGIN_DIR / f.name)


def load(builtin_names) -> tuple[list[str], list[str]]:
    """(Neu-)Lädt alle Plugins. Gibt (geladene Namen, Fehler) zurück."""
    ensure_dir()
    PLUGINS.clear()
    ERRORS.clear()
    for f in sorted(PLUGIN_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        _pending.clear()
        modname = f"spike_plugin_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(modname, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
        except Exception:
            ERRORS.append(f"{f.name}: {traceback.format_exc(limit=2).strip().splitlines()[-1]}")
            continue
        for t in _pending:
            if t["name"] in builtin_names:
                ERRORS.append(f"{f.name}: Name '{t['name']}' ist schon ein eingebautes Werkzeug")
            elif t["name"] in PLUGINS:
                ERRORS.append(f"{f.name}: '{t['name']}' gibt es schon in {PLUGINS[t['name']]['file']}")
            else:
                PLUGINS[t["name"]] = {**t, "file": f.name}
    _pending.clear()
    return list(PLUGINS), list(ERRORS)


def call(name, args, toolbox, confirm) -> str:
    p = PLUGINS[name]
    if p["confirm"]:
        shown = json.dumps(args, ensure_ascii=False)
        if not confirm(f"Plugin „{name}“ ausführen?\n\n{shown[:600]}"):
            return "Vom Nutzer abgelehnt."
    kwargs = dict(args)
    if p["wants_ctx"]:
        kwargs["ctx"] = Ctx(toolbox, confirm)
    result = p["func"](**kwargs)
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    return result
