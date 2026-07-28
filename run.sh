#!/usr/bin/env bash
# Start MedRAG. Double-click this file, or run ./run.sh in a terminal.
#
# Installs anything missing the first time, then opens the tool in a browser.
# Safe to run repeatedly.

set -euo pipefail
cd "$(dirname "$0")"

echo "Starting MedRAG…"
echo

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Python is not installed on this computer."
    echo
    echo "Install it from https://www.python.org/downloads/ (choose the latest"
    echo "version, and tick 'Add Python to PATH' if asked), then run this again."
    read -rp "Press Enter to close."
    exit 1
fi

# A local virtual environment keeps this tool's packages away from the rest of
# the system, so installing it cannot break anything else on the machine.
if [ ! -d ".venv" ]; then
    echo "First run — setting up. This takes a couple of minutes."
    "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import streamlit" >/dev/null 2>&1; then
    echo "Installing the packages MedRAG needs…"
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
fi

echo
echo "MedRAG is starting. It will open in your web browser."
echo "Leave this window open while you use it. Close it to stop."
echo

# Streamlit asks for an email on first run and blocks startup until answered.
# Pre-answering it with a blank address skips the prompt for good.
CREDS="$HOME/.streamlit/credentials.toml"
if [ ! -f "$CREDS" ]; then
    mkdir -p "$HOME/.streamlit"
    printf '[general]\nemail = ""\n' > "$CREDS"
fi

streamlit run app.py --server.headless false
