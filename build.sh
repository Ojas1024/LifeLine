#!/bin/sh
# Builds a portable Lifeline for Linux (dist/Lifeline) or macOS (dist/Lifeline.app).
# Build on a different drive than the one you're recovering from.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
"$PY" -m pip install -r requirements.txt pyinstaller
if [ "$(uname)" = "Darwin" ]; then
  "$PY" -m pip install pillow   # converts the .ico to the .icns macOS needs
  "$PY" -m PyInstaller --noconfirm --windowed --name Lifeline --icon lifeline/assets/icon.ico \
    --osx-bundle-identifier org.lifeline.recovery \
    --add-data "lifeline/assets:lifeline/assets" --collect-submodules lifeline launcher.py
  echo "Done: dist/Lifeline.app  (the app asks for your password to read drives;"
  echo "      also allow Full Disk Access in System Settings > Privacy & Security)"
else
  "$PY" -m PyInstaller --noconfirm --onefile --windowed --name Lifeline \
    --add-data "lifeline/assets:lifeline/assets" --collect-submodules lifeline launcher.py
  echo "Done: dist/Lifeline  (run it with sudo -E, or it offers to restart itself via pkexec)"
fi
