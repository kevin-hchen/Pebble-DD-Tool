#!/usr/bin/env bash
# Load research data for one asset. Run this once per asset, before using the
# tool. Takes a few minutes.
#
#   ./load_data.sh "empagliflozin" "heart failure"
#
# Loading is separated from the app on purpose: fetching is slow and only needs
# doing when the topic changes, whereas memos get generated many times against
# the same loaded data.

set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 2 ]; then
    echo "Usage: ./load_data.sh \"<asset or drug>\" \"<indication>\""
    echo
    echo "Example:"
    echo "  ./load_data.sh \"empagliflozin\" \"heart failure\""
    exit 1
fi

ASSET="$1"
INDICATION="$2"

if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

echo "Checking that the data sources are reachable…"
python -m medrag doctor || {
    echo
    echo "One or more sources could not be reached. If you are on a company or"
    echo "campus network, it may be blocking them — try a different network."
    exit 1
}

echo
echo "Loading published literature for: $ASSET $INDICATION"
python -m medrag ingest --query "$ASSET $INDICATION" -n 100 --index

echo
echo "Loading clinical trial records…"
python -m medrag trials --condition "$INDICATION" --intervention "$ASSET" -n 200

echo
echo "Done. Current contents:"
python -m medrag stats

echo
echo "You can now start the tool with ./run.sh and generate a memo for $ASSET."
