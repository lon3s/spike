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

DEFAULT_CONFIG = {
    "endpoints": [
        {"name": "PC1", "url": "http://127.0.0.1:8080", "api_key": "", "model": ""},
    ],
    "profiles": [
        {
            "name": "Allgemein",
            "system_prompt": "Du bist ein hilfreicher Assistent. Antworte knapp und auf Deutsch. Heute ist {date}.",
            "tools": ["web_search", "fetch_url"],
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
            "tools": ["web_search", "fetch_url"] + BROWSER_TOOLS,
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
            "tools": BROWSER_TOOLS + ["web_search"],
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
            "tools": ["run_shell", "read_file", "list_dir", "write_file"],
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
        except Exception as e:  # kaputte Datei nicht überschreiben
            print("Config-Fehler:", e)
    else:
        save_config(cfg)
    return cfg


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


def list_chats() -> list[dict]:
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text("utf-8"))
            out.append({"id": d["id"], "title": d.get("title", d["id"])})
        except Exception:
            pass
    return out


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
