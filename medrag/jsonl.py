"""How this project reads and writes JSONL. Both stores go through here.

This exists because of a corruption that was never a corruption. A Cochrane
abstract carried U+2028 LINE SEPARATOR inside its conflict-of-interest
statement. `json.dumps` is right not to escape it — it is a legal character
inside a JSON string — so the record was written correctly, as one physical
line. But Python's `str.splitlines()` treats U+2028 as a line break, and the
loader used `splitlines()`. So the reader chopped one valid record into eight
pieces, the first of which is an unterminated string. The file on disk was
perfect; the reader invented the damage, deterministically, at the same offset
on every run.

Two rules follow, and both matter:

**Split on newline and nothing else.** `str.splitlines()` also breaks on U+000B,
U+000C, U+001C-U+001E, U+0085, U+2028 and U+2029, none of which JSON requires to
be escaped. Any of them appearing in an abstract turns a healthy corpus into an
unreadable one. `split_lines` and `iter_lines` are the only ways a stored line
should be produced.

**Escape the separators on write anyway.** Existing files are handled by the
rule above; new records escape U+2028/U+2029 so the corpus stays safe for any
other reader too, since a JavaScript parser has the same historical trap.

The failure this prevents is worse than a crash. A tolerant loader that splits
with `splitlines()` quarantines seven perfectly good fragments and reports a
healthy 170-record corpus as 169 records with 8 unreadable ones — silent,
plausible-looking evidence loss, which is the one thing this codebase is built
throughout to refuse.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

# Spelled with chr() so this source file cannot itself contain the characters it
# exists to tame.
LINE_SEPARATOR = chr(0x2028)
PARAGRAPH_SEPARATOR = chr(0x2029)
NEWLINE = chr(0x0A)


def dumps_line(obj: Any) -> str:
    """Serialise one record as a single physical line.

    `ensure_ascii=False` keeps the corpus human-readable and compact; the two
    separators that `str.splitlines()` would trip over are escaped explicitly.
    """
    line = json.dumps(obj, ensure_ascii=False)
    return line.replace(LINE_SEPARATOR, "\\u2028").replace(
        PARAGRAPH_SEPARATOR, "\\u2029"
    )


def split_lines(text: str) -> Iterator[str]:
    """Yield non-empty stored lines, splitting on newline only.

    Never `str.splitlines()`. See the module docstring for what that costs.
    """
    for line in text.split(NEWLINE):
        if line.strip():
            yield line


def iter_lines(text: str) -> Iterator[tuple[int, str]]:
    """As `split_lines`, but with the 1-based physical line number.

    The number is what a quarantine record points at, so it counts every
    physical line, including blank ones.
    """
    for n, line in enumerate(text.split(NEWLINE), 1):
        if line.strip():
            yield n, line
