#!/usr/bin/env bash
# Double-click this file on a Mac to start MedRAG.
#
# macOS only runs double-clicked scripts when they end in .command and have the
# executable bit set. A .sh file opens in a text editor instead, which is why
# this file exists alongside run.sh.

set -uo pipefail

# Terminal opens in the home directory, not next to the file, so move here first.
cd "$(dirname "$0")" || {
    echo "Could not find the MedRAG folder. Keep this file inside it."
    read -rp "Press Enter to close."
    exit 1
}

# Without this, any failure closes the window instantly and the user sees a
# black flash with no explanation. Hold it open and say what happened.
fail() {
    echo
    echo "----------------------------------------------"
    echo "MedRAG could not start."
    echo
    echo "The error is printed above. Common causes:"
    echo "  - no internet connection during first-time setup"
    echo "  - a network that blocks software downloads"
    echo
    echo "Send the text above to whoever maintains this tool."
    echo "----------------------------------------------"
    read -rp "Press Enter to close this window."
    exit 1
}
trap fail ERR

clear
echo "=============================================="
echo "  MedRAG — diligence memo generator"
echo "=============================================="
echo

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Python is not installed on this Mac."
    echo
    echo "  1. Go to https://www.python.org/downloads/"
    echo "  2. Download and install the latest version"
    echo "  3. Double-click this file again"
    echo
    read -rp "Press Enter to close this window."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "First run — setting up. This takes two or three minutes."
    echo "You only have to wait this once."
    echo
    "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import streamlit" >/dev/null 2>&1; then
    echo "Installing the parts MedRAG needs…"
    echo
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    echo "Done."
    echo
fi

echo "Starting MedRAG. Your browser will open in a moment."
echo
echo "  Leave this black window open while you use MedRAG."
echo "  To stop it, close this window."
echo

# Streamlit asks for an email on first run and blocks startup until answered.
# Pre-answering it with a blank address skips the prompt for good.
CREDS="$HOME/.streamlit/credentials.toml"
if [ ! -f "$CREDS" ]; then
    mkdir -p "$HOME/.streamlit"
    printf '[general]\nemail = ""\n' > "$CREDS"
fi

streamlit run app.py --server.headless false

echo
read -rp "MedRAG has stopped. Press Enter to close this window."
