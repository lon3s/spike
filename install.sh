#!/usr/bin/env bash
# Installiert spike für den aktuellen Benutzer (Arch/CachyOS, Debian/Ubuntu, Fedora …).
# Aufruf:  ./install.sh            (mit Browser-Werkzeug)
#          ./install.sh --no-browser
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
APP_DIR="$PWD"

command -v python3 >/dev/null || { echo "python3 fehlt – bitte installieren."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python >= 3.10 nötig."; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "Python-venv fehlt. Debian/Ubuntu: sudo apt install python3-venv  |  Arch: ist in python enthalten"; exit 1; }

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

if [ "${1:-}" != "--no-browser" ]; then
  echo "→ installiere Playwright + Chromium (Browser-Werkzeug)"
  .venv/bin/pip install -q "playwright>=1.40"
  .venv/bin/python -m playwright install chromium || echo "⚠ Chromium-Download fehlgeschlagen – Browser-Werkzeuge gehen dann nicht."
fi

echo "→ Starter + Menüeintrag"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
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
Icon=utilities-terminal
Terminal=false
Categories=Utility;Development;
EOF

echo
echo "✅ Fertig. Starten über das Anwendungsmenü (\"spike\") oder im Terminal: spike"
echo "   Konfiguration: ~/.config/spike/config.json  (kann zwischen PCs kopiert werden)"
