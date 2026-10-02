#!/bin/sh
# Builds dist/sonyctl, a single-file executable with Python and Tk bundled.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -c "import tkinter" 2>/dev/null || { echo "Your Python lacks Tk (install tk / python3-tk) or set PYTHON=..." >&2; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pyinstaller jeepney
.venv/bin/python sonyctl.py --no-log-file selftest
.venv/bin/pyinstaller --onefile --name sonyctl --clean --noconfirm --log-level WARN sonyctl_main.py
echo "Built: dist/sonyctl"
