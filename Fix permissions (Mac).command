#!/usr/bin/env bash
# Run this only if "Start MedRAG.command" will not open.
#
# Unzipping can strip the flag that marks a file as runnable, and macOS also
# quarantines anything downloaded from the internet. This restores both.

cd "$(dirname "$0")" || exit 1

echo "Repairing MedRAG so it can run…"
echo

chmod +x *.command 2>/dev/null
chmod +x *.sh 2>/dev/null
echo "  marked the start files as runnable"

# Remove the download quarantine flag that makes macOS refuse to open the file.
if command -v xattr >/dev/null 2>&1; then
    xattr -dr com.apple.quarantine . 2>/dev/null || true
    echo "  cleared the macOS download warning"
fi

echo
echo "Done. You can now double-click 'Start MedRAG.command'."
echo
read -rp "Press Enter to close this window."
