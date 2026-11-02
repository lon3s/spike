# Spike

Leichtgewichtige Desktop-App (Python + Qt) zum Chatten mit mehreren LLM-Servern über die
**OpenAI-kompatible API**, also llama.cpp `llama-server`, Ollama, LM Studio, vLLM, OpenAI, OpenRouter und andere.
Werkzeuge (Websuche, Browser, Shell, Dateien) und System-Prompts pro Profil sind eingebaut.

## Funktionen

- **Mehrere Server und APIs**: Endpunkte mit IP:Port oder URL und optionalem API-Key anlegen, im Chat umschalten,
  Modelle werden automatisch vom Server geladen.
- **Profile**: pro Vorgang eigener System-Prompt, Temperatur und Werkzeug-Auswahl (`{date}` wird ersetzt).
- **Werkzeuge**: `web_search` (DuckDuckGo), `fetch_url`, Browser (Playwright: öffnen, lesen, klicken, tippen,
  zurück), `run_shell`, `read_file`, `list_dir`, `write_file`. Shell-Befehle und Schreibzugriffe muss man bestätigen.
- **Kontext sparen**:
  - Nur die Werkzeuge des aktiven Profils werden ans Modell geschickt.
  - Werkzeug-Ausgaben sind begrenzt (Standard 6000 Zeichen, mit `offset` kann das Modell weiterlesen).
  - Ältere Werkzeug-Ausgaben werden automatisch gekürzt.
  - Das „Denken“ (`reasoning_content`) wird nur angezeigt und nicht zurückgeschickt.
  - Wird der Kontext voll, fallen die ältesten Runden weg.
  - Die Kontextgröße kommt vom Server (`/props`), die Auslastung zeigt die Statusleiste.
- Chats werden lokal gespeichert (`~/.local/share/spike/chats`).

## Installation (auf jedem PC)

```bash
git clone <repo-url> ~/projects/spike
cd ~/projects/spike
./install.sh            # oder ./install.sh --no-browser
spike
```

Die Konfiguration liegt in `~/.config/spike/config.json`. Diese Datei einfach auf die anderen PCs kopieren,
dann sind dort dieselben Endpunkte und Profile eingerichtet.

## llama.cpp-Server im Netzwerk erreichbar machen

```bash
llama-server -m modell.gguf --host 0.0.0.0 --port 8080 --jinja -c 16384
# optional mit Schlüssel:  --api-key meinschluessel
```

- `--host 0.0.0.0` ist nötig, damit andere PCs zugreifen können. Ohne diese Option lauscht der Server nur lokal.
- `--jinja` wird für Tool-Calling (Werkzeuge) gebraucht.
- In der App: ⚙ Einstellungen → Endpunkte → `http://192.168.x.y:8080` eintragen → „Verbindung testen“.

Ollama: `http://ip:11434`. OpenAI: `https://api.openai.com` plus API-Key.

## Bedienung

- Enter sendet, Shift+Enter macht eine neue Zeile, Strg+N startet einen neuen Chat, Esc stoppt.
- **Strg+K**: Kompakt-Modus (nur Verlauf und Eingabe), **Strg+B**: Seitenleiste ein/aus, ◐: hell/dunkel.
- „Stopp“ bricht die laufende Antwort oder Werkzeugkette ab.
