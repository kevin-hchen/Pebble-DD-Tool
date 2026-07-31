#!/usr/bin/env bash
# Launch the Streamlit app with the libomp workaround the local torch/faiss
# install needs on macOS. Used by .claude/launch.json.
set -e
cd "$(dirname "$0")/.."
export KMP_DUPLICATE_LIB_OK=TRUE
exec .venv/bin/streamlit run app.py --server.port "${PORT:-8501}" --server.headless true
