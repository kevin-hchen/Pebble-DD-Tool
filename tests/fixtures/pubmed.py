"""Real PubMed E-utilities efetch XML fixtures.

Captured from live NCBI responses to pin down the shapes that broke code once
and could silently break it again. Each fixture is the raw XML returned by
`efetch.fcgi?db=pubmed&id=<PMID>&retmode=xml` at capture time - do not edit by
hand, replace with a fresh capture instead.

- PMID 36571459 : the Results section chunks to exactly two pieces with a
  short tail. This tripped the runt-tail merge, which evaluated
  `chunks.pop()` inside an f-string before the LHS `chunks[-2] =` store, so a
  2-chunk list dropped to length 1 and IndexError'd.
"""

from __future__ import annotations

from pathlib import Path

_FIXTURE_DIR = Path(__file__).parent


def efetch_xml(pmid: str) -> bytes:
    """Return the captured efetch XML bytes for a given PMID."""
    path = _FIXTURE_DIR / f"pubmed_{pmid}_efetch.xml"
    return path.read_bytes()
