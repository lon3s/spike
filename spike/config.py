"""Konfiguration und Chat-Speicher (JSON-Dateien)."""
import copy
import json
import os
import time
import uuid
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "spike"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "spike"
CONFIG_FILE = CONFIG_DIR / "config.json"
CHATS_DIR = DATA_DIR / "chats"

BROWSER_TOOLS = ["browser_open", "browser_read", "browser_click", "browser_type", "browser_back"]

ALL_PROFILE = {
    "name": "Alles",
    "system_prompt": (
        "Du bist spike, ein Assistent auf dem Linux-PC des Nutzers (CachyOS, KDE Plasma, Wayland). Heute ist {date}. "
        "Antworte knapp und auf Deutsch. Du hast Werkzeuge für Websuche und Browser, Shell, Dateien, Python, "
        "PC-Steuerung (Programme/Dateien öffnen, Zwischenablage, Lautstärke, Medien) und ein Gedächtnis. "
        "Nutze ein Werkzeug nur, wenn es nötig ist; einfache Fragen beantwortest du direkt. "
        "Vor Änderungen am System sagst du kurz, was du vorhast. Bei Rechnungen und Datenauswertung nimm Python. "
        "Für aktuelle Informationen suche im Web und nenne die Quellen."
    ),
    "tools": ["*"],
    "temperature": 0.5,
    "max_tokens": 0,
}

DEFAULT_CONFIG = {
    "endpoints": [
        {"name": "PC1", "url": "http://127.0.0.1:8080", "api_key": "", "model": ""},
    ],
    "profiles": [
        ALL_PROFILE,
        {
            "name": "Allgemein",
            "system_prompt": "Du bist ein hilfreicher Assistent. Antworte knapp und auf Deutsch. Heute ist {date}.",
            "tools": ["web_search", "fetch_url", "ask_user"],
            "temperature": 0.7,
            "max_tokens": 0,
        },
        {
            "name": "Recherche",
            "system_prompt": (
                "Du bist ein Recherche-Assistent. Heute ist {date}. Nutze web_search, um Quellen zu finden, "
                "und fetch_url oder den Browser, um sie zu lesen. Nenne am Ende die Quellen-URLs. "
                "Rufe nur so viele Seiten ab wie nötig."
            ),
            "tools": ["web_search", "fetch_url", "ask_user"] + BROWSER_TOOLS,
            "temperature": 0.4,
            "max_tokens": 0,
        },
        {
            "name": "Browser-Agent",
            "system_prompt": (
                "Du steuerst einen Webbrowser. Heute ist {date}. browser_open liefert interaktive Elemente "
                "als [Nummer]; benutze diese Nummern für browser_click und browser_type. "
                "Arbeite Schritt für Schritt und fasse am Ende das Ergebnis zusammen."
            ),
            "tools": BROWSER_TOOLS + ["web_search", "ask_user"],
            "temperature": 0.3,
            "max_tokens": 0,
        },
        {
            "name": "System / Shell",
            "system_prompt": (
                "Du bist ein Linux-Admin-Assistent (Arch/CachyOS, fish/zsh). Heute ist {date}. "
                "Du kannst Shell-Befehle ausführen und Dateien lesen/schreiben; der Nutzer bestätigt jede "
                "Aktion. Erkläre kurz, was du vorhast, bevor du etwas veränderst."
            ),
            "tools": ["run_shell", "read_file", "list_dir", "write_file", "ask_user"],
            "temperature": 0.2,
            "max_tokens": 0,
        },
        {
            "name": "Nur Chat (kein Werkzeug)",
            "system_prompt": "Du bist ein hilfreicher Assistent. Heute ist {date}.",
            "tools": [],
            "temperature": 0.8,
            "max_tokens": 0,
        },
    ],
    "settings": {
        "tool_output_max_chars": 6000,
        "keep_full_tool_results": 2,
        "old_tool_result_chars": 300,
        "max_steps": 15,
        "browser_headless": True,
        "theme": "system",
        "compact": False,
        "default_n_ctx": 8192,
        # Leuchtrahmen um das Tray-Popup
        "popup_glow": True,
        "popup_glow_style": "lauflicht",          # "lauflicht" oder "verlauf"
        "popup_glow_colors": ["#8b7cff", "#22d3ee", "#f472b6"],
        "popup_glow_speed": 4.0,                  # Sekunden pro Umlauf
        "popup_glow_width": 2,                    # Pixel
        # Gedächtnis
        "memory_enabled": True,
        "memory_prompt_chars": 1500,              # so viel Gedächtnis geht höchstens in den System-Prompt
        # Kontext: bei Bedarf älteren Verlauf vom Modell zusammenfassen lassen
        "auto_compact": True,
        "compact_at_percent": 70,                 # ab dieser Auslastung (vom nutzbaren Kontext)
        "compact_keep_percent": 30,               # so viel vom Ende bleibt wörtlich
        "python_timeout": 120,
        "perm_mode": "fragen",                    # Freigaben: fragen | dateien | alles | lesen
        "attach_max_chars": 30000,                # so viel Text pro angehängter Datei
        # MCP-Server: [{"name", "command", "env": {}, "enabled": true, "confirm": "schreibend"|"immer"|"nie"}]
        "mcp_servers": [],
        # Sprache
        "stt_python": "", "stt_model": "", "stt_device": "auto", "stt_language": "de", "stt_url": "",
        "stt_autosend": False,
        "tts_url": "http://127.0.0.1:8000/tts", "tts_voice": "senol1", "tts_language": "de",
        "tts_start": "cd ~/voxcpm-tts && ./.venv/bin/python api.py", "tts_auto": False,
        "tts_device": "auto", "tts_min_vram_mb": 6000,      # auto: GPU wenn so viel frei, sonst CPU
        "tts_engine": "auto",                     # auto: VoxCPM wenn GPU frei, sonst Piper; oder voxcpm/piper
        "piper_model": "",                        # leer = erstes *.onnx in ~/.local/share/spike/piper
        "drive_index": True,                      # Dateisuche: /mnt täglich im Hintergrund indizieren
        # Handy-Oberfläche (Webserver im Heimnetz)
        "web_enabled": False, "web_port": 8765, "web_https": True,
    },
}


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text("utf-8"))
            for key in ("endpoints", "profiles"):
                if isinstance(user.get(key), list):
                    cfg[key] = user[key]
            cfg["settings"].update(user.get("settings") or {})
            if _migrate(cfg):
                save_config(cfg)
        except Exception as e:  # kaputte Datei nicht überschreiben
            print("Config-Fehler:", e)
    else:
        save_config(cfg)
    return cfg


def _migrate(cfg) -> bool:
    """Einmalige Ergänzungen für ältere Konfigurationen. Gibt True zurück, wenn etwas geändert wurde."""
    done = set(cfg["settings"].get("migrations") or [])
    changed = False
    if "profile_alles" not in done:
        if not any(p.get("name") == ALL_PROFILE["name"] for p in cfg["profiles"]):
            cfg["profiles"].insert(0, copy.deepcopy(ALL_PROFILE))
        done.add("profile_alles")
        changed = True
    if "ask_user" not in done:    # Rückfrage-Werkzeug für alle Profile, die schon Werkzeuge haben
        for p in cfg["profiles"]:
            t = p.get("tools")
            if isinstance(t, list) and t and "*" not in t and "ask_user" not in t:
                t.append("ask_user")
        done.add("ask_user")
        changed = True
    if changed:
        cfg["settings"]["migrations"] = sorted(done)
    return changed


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    tmp.replace(CONFIG_FILE)


# ---------- Chats ----------

def new_chat(endpoint="", model="", profile="") -> dict:
    return {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
        "title": "Neuer Chat",
        "endpoint": endpoint,
        "model": model,
        "profile": profile,
        "messages": [],
    }


def list_chats(archived: bool = False, with_text: bool = False) -> list[dict]:
    """Chats, neueste zuerst. archived=True liefert nur das Archiv, sonst nur die aktiven.
    with_text=True: zusätzlich "text" (Titel + Nachrichten, klein geschrieben) für die Suche."""
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    files = [(f, f.stat().st_mtime) for f in CHATS_DIR.glob("*.json")]
    for f, mtime in sorted(files, key=lambda x: x[1], reverse=True):
        try:
            d = json.loads(f.read_text("utf-8"))
            if bool(d.get("archived")) == archived:
                c = {"id": d["id"], "title": d.get("title", d["id"]), "mtime": mtime}
                if with_text:
                    parts = [c["title"]] + [str(m.get("_text") or m.get("content") or "")
                                            for m in d.get("messages", []) if m.get("role") in ("user", "assistant")]
                    c["text"] = "\n".join(parts)[:40000].lower()
                out.append(c)
        except Exception:
            pass
    return out


def update_chat_meta(chat_id: str, **fields) -> dict:
    """Titel/Archiv-Status ändern, ohne die Reihenfolge (Änderungszeit) zu verschieben."""
    f = CHATS_DIR / f"{chat_id}.json"
    st = f.stat()
    d = json.loads(f.read_text("utf-8"))
    for k, v in fields.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    save_chat(d)
    os.utime(f, (st.st_atime, st.st_mtime))
    return d


def load_chat(chat_id: str) -> dict:
    return json.loads((CHATS_DIR / f"{chat_id}.json").read_text("utf-8"))


def save_chat(chat: dict) -> None:
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    f = CHATS_DIR / f"{chat['id']}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(f)


def delete_chat(chat_id: str) -> None:
    f = CHATS_DIR / f"{chat_id}.json"
    if f.exists():
        f.unlink()
