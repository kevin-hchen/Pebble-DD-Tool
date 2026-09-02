"""The on-disk names of the stores. A leaf module, importing nothing.

Split out of `pipeline.py` because the public service needs these four strings
and nothing else from that module — and importing `pipeline` for a filename
pulled in `embeddings`, which pulls `numpy`, which pulls the whole vector stack.
The public image would have had to ship faiss, torch and sentence-transformers
to serve a page that never embeds anything: a larger attack surface, a slower
pull and a longer list of CVEs to answer for, all for code the service cannot
run.

`pipeline.py` re-exports these, so every existing caller is unchanged and there
is still exactly one definition of each name.
"""

from __future__ import annotations

CORPUS_FILE = "corpus.jsonl"
TRIALS_DB = "trials.db"
FDA_DB = "fda.db"
DRUGS_DB = "drugs.db"
