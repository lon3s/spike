#!/usr/bin/env bash
# Installiert spike für den aktuellen Benutzer (Arch/CachyOS, Debian/Ubuntu, Fedora, openSUSE …).
#
#   ./install.sh                 Grundinstallation (mit Browser-Werkzeug)
#   ./install.sh --all           alles: Zusatzprogramme, Autostart, Piper-Stimme, Diktieren
#
# Einzelne Schalter (beliebig kombinierbar):
#   --deps          fehlende Zusatzprogramme per Paketmanager installieren (fragt nach dem sudo-Passwort)
#   --autostart     beim Anmelden im Tray starten
#   --voice         Piper + deutsche Stimme „thorsten-high“ (Vorlesen ohne GPU, ~110 MB)
#   --stt           faster-whisper fürs Diktieren (Modell ~1,6 GB lädt beim ersten Diktieren)
#   --no-browser    ohne Playwright/Chromium (spart ~150 MB)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
APP_DIR="$PWD"
ARGS=" $* "
has() { [[ "$ARGS" == *" $1 "* || "$ARGS" == *" --all "* ]]; }

# ---------- Python ----------
command -v python3 >/dev/null || { echo "❌ python3 fehlt – bitte über den Paketmanager installieren."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "❌ Python >= 3.10 nötig."; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "❌ Python-venv fehlt. Debian/Ubuntu: sudo apt install python3-venv  |  Fedora: sudo dnf install python3"; exit 1; }

echo "→ virtuelle Umgebung (.venv)"
# --system-site-packages: nutzt ein vorhandenes System-PySide6 (spart ~200 MB Download)
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -q --upgrade pip
if .venv/bin/python -c 'import PySide6' 2>/dev/null; then
  echo "→ PySide6 bereits vorhanden"
else
  echo "→ installiere PySide6"
  .venv/bin/pip install -q "PySide6>=6.5"
fi

if [[ "$ARGS" != *" --no-browser "* ]]; then
  echo "→ installiere Playwright + Chromium (Browser-Werkzeug)"
  .venv/bin/pip install -q "playwright>=1.40"
  .venv/bin/python -m playwright install chromium || echo "⚠ Chromium-Download fehlgeschlagen – Browser-Werkzeuge gehen dann nicht."
fi

# ---------- Starter, Menüeintrag, Symbol ----------
echo "→ Starter + Menüeintrag"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cat > "$HOME/.local/bin/spike" <<EOF
#!/usr/bin/env bash
cd "$APP_DIR" && exec "$APP_DIR/.venv/bin/python" -m spike "\$@"
EOF
chmod +x "$HOME/.local/bin/spike"
cat > "$HOME/.local/share/applications/spike.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=spike
Comment=Chat mit lokalen und entfernten LLMs (OpenAI-API) mit Werkzeugen
Exec=$HOME/.local/bin/spike
Icon=spike
Terminal=false
Categories=Utility;Development;
EOF
cp -f "$APP_DIR/spike/assets/spike.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/spike.svg"

if has --autostart; then
  echo "→ Autostart im Tray eingerichtet (entfernen: rm ~/.config/autostart/spike.desktop)"
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/spike.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=spike
Exec=$HOME/.local/bin/spike --tray
Icon=spike
EOF
fi

# ---------- Zusatzprogramme ----------
# Programm → Funktion in spike. Fehlt eines, geht nur diese Funktion nicht.
declare -A WHAT=(
  [plocate]="Dateisuche (Systemindex)"   [fd]="Dateisuche im Home-Ordner"  [rg]="grep in Projekten"
  [wl-copy]="Zwischenablage"             [notify-send]="Erinnerungen"     [kdialog]="sudo-Passwortabfrage"
  [spectacle]="/screenshot"              [pw-record]="Diktieren (Aufnahme)" [wpctl]="Lautstärke"
  [qdbus6]="Mediensteuerung"
)
if command -v pacman >/dev/null; then
  PM="sudo pacman -S --needed"
  declare -A PKG=([plocate]=plocate [fd]=fd [rg]=ripgrep [wl-copy]=wl-clipboard [notify-send]=libnotify
    [kdialog]=kdialog [spectacle]=spectacle [pw-record]=pipewire-audio [wpctl]=wireplumber [qdbus6]=qt6-tools)
elif command -v apt-get >/dev/null; then
  PM="sudo apt-get install -y"
  declare -A PKG=([plocate]=plocate [fd]=fd-find [rg]=ripgrep [wl-copy]=wl-clipboard [notify-send]=libnotify-bin
    [kdialog]=kdialog [spectacle]=kde-spectacle [pw-record]=pipewire-bin [wpctl]=wireplumber [qdbus6]=qdbus-qt6)
elif command -v dnf >/dev/null; then
  PM="sudo dnf install -y"
  declare -A PKG=([plocate]=plocate [fd]=fd-find [rg]=ripgrep [wl-copy]=wl-clipboard [notify-send]=libnotify
    [kdialog]=kdialog [spectacle]=spectacle [pw-record]=pipewire-utils [wpctl]=wireplumber [qdbus6]=qt6-qttools)
elif command -v zypper >/dev/null; then
  PM="sudo zypper install -y"
  declare -A PKG=([plocate]=plocate [fd]=fd [rg]=ripgrep [wl-copy]=wl-clipboard [notify-send]=libnotify-tools
    [kdialog]=kdialog [spectacle]=spectacle [pw-record]=pipewire-tools [wpctl]=wireplumber [qdbus6]=qt6-tools-qdbus)
else
  PM=""; declare -A PKG=()
fi

# Debian/Ubuntu nennen fd „fdfind“ → Verknüpfung, damit spike es findet
if ! command -v fd >/dev/null && command -v fdfind >/dev/null; then
  ln -sf "$(command -v fdfind)" "$HOME/.local/bin/fd"
fi

missing=(); pkgs=()
for t in plocate fd rg wl-copy notify-send kdialog spectacle pw-record wpctl qdbus6; do
  if ! command -v "$t" >/dev/null && ! [ -x "$HOME/.local/bin/$t" ]; then
    missing+=("$t"); [ -n "${PKG[$t]:-}" ] && pkgs+=("${PKG[$t]}")
  fi
done
if ((${#missing[@]})); then
  if has --deps && [ -n "$PM" ] && ((${#pkgs[@]})); then
    echo "→ installiere Zusatzprogramme: ${pkgs[*]}"
    $PM "${pkgs[@]}" || echo "⚠ Nicht alle Pakete ließen sich installieren – spike läuft trotzdem."
    if [[ " ${pkgs[*]} " == *" plocate "* ]]; then sudo updatedb 2>/dev/null || true; fi
    if ! command -v fd >/dev/null && command -v fdfind >/dev/null; then ln -sf "$(command -v fdfind)" "$HOME/.local/bin/fd"; fi
  else
    echo
    echo "ℹ Optionale Zusatzprogramme fehlen (spike läuft trotzdem, nur diese Funktionen nicht):"
    for t in "${missing[@]}"; do printf "   %-12s %s\n" "$t" "${WHAT[$t]}"; done
    if [ -n "$PM" ] && ((${#pkgs[@]})); then
      echo "   Nachinstallieren:  $PM ${pkgs[*]}"
      echo "   (oder ./install.sh --deps)"
    fi
  fi
fi

# ---------- Sprache ----------
if has --voice; then
  echo "→ Piper + Stimme thorsten-high (Vorlesen)"
  .venv/bin/pip install -q piper-tts || echo "⚠ piper-tts ließ sich nicht installieren."
  VDIR="$HOME/.local/share/spike/piper"; mkdir -p "$VDIR"
  BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high"
  for f in de_DE-thorsten-high.onnx de_DE-thorsten-high.onnx.json; do
    [ -s "$VDIR/$f" ] || curl -fL# -o "$VDIR/$f" "$BASE/$f" || echo "⚠ Download von $f fehlgeschlagen."
  done
fi
if has --stt; then
  echo "→ faster-whisper (Diktieren; das Sprachmodell lädt beim ersten Diktieren, ~1,6 GB)"
  .venv/bin/pip install -q faster-whisper || echo "⚠ faster-whisper ließ sich nicht installieren."
fi

# ---------- Fertig ----------
echo
echo "✅ Fertig. Starten über das Anwendungsmenü („spike“) oder im Terminal: spike"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "⚠ ~/.local/bin ist nicht im PATH – im Terminal dann: ~/.local/bin/spike"
     echo "   (dauerhaft: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc  bzw. ~/.zshrc)";;
esac
CFG="$HOME/.config/spike/config.json"
if [ -f "$CFG" ]; then
  echo "   Konfiguration vorhanden: $CFG"
else
  echo "   Erster Start: ⚙ Einstellungen → Endpunkte → Server-Adresse eintragen (z. B. http://192.168.1.50:8080)"
  echo "   oder die config.json eines anderen PCs nach ~/.config/spike/ kopieren."
fi
echo "   Hilfe im Programm: F1"
