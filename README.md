# spike

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
- **Langer Verlauf** (z. B. beim Programmieren): Ab 70 % Auslastung fasst das Modell den älteren Teil selbst zusammen,
  auch mitten in einer Werkzeug-Kette. Die neuesten ~30 % bleiben wörtlich, Ziel, Pfade und offene Punkte bleiben in der
  Zusammenfassung erhalten. Einstellbar unter ⚙ → Gedächtnis & Kontext.
- **Gedächtnis**: „Merk dir …“ speichert kurze Fakten (`~/.local/share/spike/memory.json`). Pro Chat geht nur ein
  begrenzter Auszug in den Prompt (Standard 1500 Zeichen): angeheftete Einträge, dann die zur ersten Nachricht passenden,
  dann die neuesten. Den Rest sucht das Modell bei Bedarf mit `memory_search`. Der Auszug bleibt pro Chat gleich,
  damit der Prompt-Cache von llama.cpp greift. Einträge verwalten: ⚙ → Gedächtnis & Kontext.
- **Dateisuche** (`find_files`, ohne Bestätigung): Systemindex (`plocate`), frische Suche im Home-Ordner (`fd`) und ein
  eigener Index für `/mnt` (NAS, Laufwerke) in `~/.cache/spike/mnt.db`. spike erneuert ihn täglich im Hintergrund
  (niedrigste Priorität, abschaltbar unter ⚙ → Allgemein, sofort mit `/index`). Suche dauert so ~0,1 s.
- **Programmieren**: `read_file` mit Zeilennummern und Abschnitten, `edit_file` ersetzt gezielt Stellen (Freigabe zeigt
  einen farbigen Diff, danach steht die Änderung als Diff-Karte im Chat), `grep` (ripgrep) durchsucht Projekte.
  Relative Pfade gelten ab dem aktuellen Ordner (`!cd projekt`).
- **Anhänge**: 📎, Drag & Drop oder Strg+V (Bilder aus der Zwischenablage). Text-/Code-Dateien und PDFs gehen als Text
  ans Modell (max. 30 000 Zeichen pro Datei), Bilder als Bild – sofern der Server Bilder versteht (llama.cpp mit
  `--mmproj`, erkannt über `/props`). Nur die letzten 3 Bilder eines Chats werden mitgeschickt.
- **Screenshots**: `/screenshot [Frage]` blendet das Popup aus, fotografiert den Bildschirm (Spectacle) und hängt das
  Bild an; das Modell kann mit dem Werkzeug `screenshot` auch selbst nachsehen.
- **Profil „Alles“**: alle Werkzeuge, das Modell entscheidet selbst. Weitere Werkzeuge: `python_run` (mit Bestätigung)
  und `pc` (Dateien/Programme/URLs öffnen, Zwischenablage, Benachrichtigung, Lautstärke über `wpctl`,
  Medien über MPRIS/`qdbus6`).
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

## Eigene Werkzeuge (Plugins)

Jede `.py`-Datei in `~/.config/spike/tools/` wird beim Start geladen. Ein Werkzeug sieht so aus:

```python
from spike.plugins import tool

@tool("Aktuelles Wetter für einen Ort.", params={"ort": "Stadt"}, label="Wetter")
def wetter(ort: str, ctx) -> str:
    return ctx.http_get(f"https://wttr.in/{ort}?format=3", headers={"User-Agent": "curl/8"})
```

- Die Parameter liest spike aus der Funktion (`str`, `int`, `float`, `bool`, `list`). Parameter ohne Standardwert sind Pflicht.
- Über `ctx` hat ein Plugin Zugriff auf `http_get`, `html_to_text`, `run("befehl")`, `confirm("Frage?")` und `settings`.
- Mit `confirm=True` fragt spike vor jedem Aufruf nach. Die Rückgabe wird automatisch gekürzt.
- Danach: ⚙ Einstellungen → Allgemein → „Plugins neu laden“, Einstellungen neu öffnen und das Werkzeug im Profil anhaken.
- Dateien, die mit `_` beginnen, werden ignoriert. `_vorlage.py` ist die kommentierte Vorlage.
- Mitgeliefert: `wetter`, `rechner` (exakte Mathematik) und `system_info` (CPU, RAM, Platten, GPU). Sie stehen unter `examples/plugins/` und
  werden beim ersten Start in den Ordner kopiert.

## Tray und Popup

spike hat ein Symbol unten rechts in der Taskleiste (Systemabschnitt):

- **Linksklick**: Das Chat-Popup erscheint unten rechts. Ein Klick daneben schließt es wieder.
- **Rechtsklick**: Menü mit Großes Fenster, Neuer Chat und Beenden.
- **Mittelklick**: großes Fenster.
- Im Popup öffnet ⤢ denselben Chat im großen Fenster, Esc schließt das Popup.
- 📌 im Popup heftet es an: Es bleibt offen, auch wenn du woanders klickst. Ein zweiter Klick löst es wieder.
- Das große Fenster schließen beendet spike nicht, es läuft im Tray weiter. Beenden geht über das Tray-Menü.
- Es läuft immer nur ein spike. `spike` holt das Fenster nach vorn, `spike --popup` schaltet das Popup um,
  `spike --tray` startet nur im Tray, `spike --quit` beendet.
- Tastenkürzel (KDE): Systemeinstellungen → Tastatur → Kurzbefehle → „Neu“ → Befehl → `spike --popup`.
- Autostart: `./install.sh --autostart` (Entfernen: `rm ~/.config/autostart/spike.desktop`).
- Unter KDE/Wayland wird das Popup per layer-shell angedockt. Auf anderen Desktops wird es unten rechts platziert.

## Bedienung

### Freigaben und Terminal

- Will das Modell etwas am System tun (Shell, Datei schreiben, Python, Öffnen), erscheint im Chat eine Karte:
  **Erlauben**, **Immer: …** (in diesem Chat nicht mehr fragen, bei Shell nur für dasselbe Programm, z. B. `git`)
  oder **Ablehnen**. Schreibst du stattdessen etwas ins Eingabefeld, wird abgelehnt und das Modell bekommt deinen
  Hinweis („nein, nimm lieber …“).
- Shell-Befehle erscheinen als Terminal-Block mit Live-Ausgabe und Exit-Code. Stopp/Esc bricht einen Befehl ab.
- `!befehl` führt selbst etwas im Terminal aus (wie in Claude Code). Die Ausgabe sieht das Modell bei der nächsten
  Frage. `cd` bleibt erhalten, auch für die Befehle des Modells. Interaktive Programme (sudo-Passwort, vim, less)
  gehen nicht. `sudo` geht: Das Passwort fragt ein KDE-Fenster ab (`kdialog`), es geht direkt an sudo und nie an
  das Modell.
- **Denken an/aus** pro Profil (⚙ → Profile oder `/denken an|aus`): Aus ist bei einfachen Aufgaben deutlich schneller
  (Dateisuche: 9 s statt 55 s). Wirkt bei llama.cpp mit Qwen & Co. (`enable_thinking`).

### Befehle

`/` im Eingabefeld zeigt eine Liste. ↑/↓ wählen, Tab ergänzt, Enter führt aus. Bei `/profil`, `/modell` und
`/server` werden nach dem Leerzeichen die passenden Namen vorgeschlagen.

| Befehl | Wirkung |
|---|---|
| `/new` (`/neu`, `/clear`) | neuer Chat |
| `/compact [Hinweis]` (`/compress`) | Verlauf jetzt zusammenfassen, macht Kontext frei |
| `/kontext` | Kontext-Belegung anzeigen |
| `/profil <Name>`, `/modell <Name>`, `/server <Name>` | wechseln (auch im Popup, wo die Kopfzeile fehlt) |
| `/screenshot [Frage]` | Bildschirmfoto anhängen |
| `/merk <Text>` | ins Gedächtnis schreiben |
| `/denken [an\|aus\|standard]` | Nachdenken des Modells für das Profil schalten |
| `/stop` | Antwort abbrechen |
| `/gross` | Chat im großen Fenster öffnen |
| `/einstellungen` | Einstellungen |
| `/index` | Laufwerks-Index für die Dateisuche erneuern |
| `/quit` (`/beenden`) | spike beenden |
| `/hilfe` | alle Befehle |

Im Popup und im Kompakt-Modus zeigt eine Zeile unter dem Eingabefeld die Kontext-Belegung, die Geschwindigkeit
(live während der Antwort) sowie Modell und Profil.


- Enter sendet, Shift+Enter macht eine neue Zeile, Strg+N startet einen neuen Chat, Esc stoppt.
- **Strg+K**: Kompakt-Modus (nur Verlauf und Eingabe), **Strg+B**: Seitenleiste ein/aus, ◐: hell/dunkel.
- „Stopp“ bricht die laufende Antwort oder Werkzeugkette ab.
