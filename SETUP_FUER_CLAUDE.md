# Anleitung für Claude Code: LLM Agent auf diesem PC einrichten und auf GitHub hochladen

Dieser Ordner enthält die App **LLM Agent**, eine Python/Qt-Desktop-App zum Chatten mit OpenAI-kompatiblen
LLM-Servern mit Werkzeugen. Er ist bereits ein Git-Repo mit Branch `main` und mindestens einem Commit.
Der Ordner wurde von einem anderen PC hierher kopiert.

Bitte führe die folgenden Schritte nacheinander aus. Frag den Nutzer, bevor du etwas veröffentlichst.

## 1. Prüfen

```bash
git status            # muss "main" zeigen; uncommittete Änderungen dem Nutzer melden
git log --oneline
ls                    # llm_agent/, install.sh, README.md, requirements.txt
```

Falls `.venv/` mitkopiert wurde: löschen (`rm -rf .venv`). Die venv ist an den alten PC gebunden.

## 2. Privates GitHub-Repo anlegen und hochladen

1. Prüfen, ob `gh` installiert ist (`which gh`). Wenn nicht, den Nutzer bitten, `! sudo pacman -S github-cli`
   (Arch/CachyOS) bzw. `! sudo apt install gh` (Debian/Ubuntu) auszuführen.
2. Anmeldung prüfen: `gh auth status`. Wenn nicht angemeldet: Nutzer soll `! gh auth login` ausführen.
3. Repo **privat** anlegen und pushen (vorher kurz beim Nutzer bestätigen):
   ```bash
   gh repo create llm-agent --private --source=. --remote=origin --push
   ```
4. Die Repo-URL dem Nutzer melden. Auf weiteren PCs geht es dann mit
   `git clone https://github.com/<nutzer>/llm-agent ~/projects/llm-agent`.

## 3. App installieren

```bash
./install.sh             # mit Browser-Werkzeug (Playwright + Chromium, ca. 120 MB)
# ./install.sh --no-browser
```

Das Skript legt `.venv` an, installiert PySide6 (falls nicht systemweit vorhanden) und Playwright und erstellt
den Befehl `llm-agent` (`~/.local/bin`) sowie einen Menüeintrag „LLM Agent“.

## 4. Endpunkte konfigurieren

Die Konfiguration liegt in `~/.config/llm-agent/config.json` und wird beim ersten Start angelegt.
Bekannter Server im Netz:

| Name   | URL                           | Hinweis                                    |
|--------|-------------------------------|--------------------------------------------|
| PC-185 | http://192.168.178.185:9070   | llama.cpp, Qwen 27B, 131k Kontext, Tools ok |

Eintragen entweder in der App (⚙ Einstellungen → Endpunkte → „Verbindung testen“) oder direkt in der JSON-Datei
unter `"endpoints"`:

```json
{"name": "PC-185", "url": "http://192.168.178.185:9070", "api_key": "", "model": ""}
```

Verbindung testen: `curl -s http://192.168.178.185:9070/v1/models`

Wenn auf diesem PC selbst ein llama-server laufen soll, der von den anderen PCs erreichbar ist:
`llama-server -m <modell.gguf> --host 0.0.0.0 --port <port> --jinja -c <kontext>`
(`--jinja` wird für Tool-Calling gebraucht). Eventuell muss der Port in der Firewall freigegeben werden.

## 5. Start

```bash
llm-agent
```

Weitere Details stehen in `README.md`.
