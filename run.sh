#!/bin/sh
# Starts Lifeline on Linux or macOS. Reading drives needs administrator rights;
# the app offers to restart itself with them, or run: sudo -E ./run.sh
cd "$(dirname "$0")" || exit 1
PY=${PYTHON:-python3}
"$PY" -c "import PyQt6" 2>/dev/null || "$PY" -m pip install --user -r requirements.txt
exec "$PY" -m lifeline "$@"
