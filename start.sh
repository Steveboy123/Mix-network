#!/usr/bin/env sh
# Start MixNet on macOS or Linux:  ./start.sh   (add --demo to try it with simulated connections)
cd "$(dirname "$0")" || exit 1
PY=python3
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is required. macOS: run 'xcode-select --install'. Linux: install python3 from your package manager."
  exit 1
fi
if ! "$PY" -c "import psutil" 2>/dev/null; then
  if [ ! -x .venv/bin/python ]; then
    echo "Setting up a private Python environment in .venv (one time)..."
    "$PY" -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt
  fi
  if [ -x .venv/bin/python ] && .venv/bin/python -c "import psutil" 2>/dev/null; then
    PY=.venv/bin/python
  else
    echo "Couldn't install psutil automatically. Install it, then run this again:"
    echo "  Debian/Ubuntu:  sudo apt install python3-psutil"
    echo "  Fedora:         sudo dnf install python3-psutil"
    echo "  Other:          python3 -m pip install psutil"
    exit 1
  fi
fi
exec "$PY" -m mixnet "$@"
