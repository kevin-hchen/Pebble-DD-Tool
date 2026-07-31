"""Regenerate tests/fixtures/corpus_broken.jsonl.

The corpus that actually crashed was not kept, so this reconstructs a file with
the same failure signature as the reported traceback:

    json.decoder.JSONDecodeError: Unterminated string starting at:
    line 1 column 4409 (char 4408)

The last record is cut off in the middle of its `text` value with no closing
quote and no newline, which is what an append killed part-way through leaves on
disk. Everything before it is intact.

    python tests/fixtures/make_broken_corpus.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("corpus_broken.jsonl")

# The offset the reported traceback names: the opening quote of the unterminated
# string sits at char 4408, i.e. column 4409 counting from one.
BREAK_AT = 4408

ABSTRACT = (
    "Background: Empagliflozin reduces cardiovascular death in patients with "
    "heart failure. Methods: We randomised 3730 patients with chronic heart "
    "failure and a reduced ejection fraction to empagliflozin 10 mg once daily "
    "or placebo. Results: The primary outcome occurred in 361 of 1863 patients "
    "in the empagliflozin group and in 462 of 1867 in the placebo group. "
    "Conclusions: Empagliflozin lowered the combined risk of cardiovascular "
    "death or hospitalisation for heart failure. "
)


def record(doc_id: str, title: str, text: str) -> dict:
    """Field order matches Document.to_dict (dataclasses.asdict)."""
    return {
        "doc_id": doc_id,
        "title": title,
        "text": text,
        "source": "pubmed",
        "authors": ["Packer M", "Anker SD", "Butler J"],
        "journal": "N Engl J Med",
        "year": "2020",
        "url": "https://pubmed.ncbi.nlm.nih.gov/" + doc_id + "/",
        "meta": {"publication_types": ["Randomized Controlled Trial"]},
    }


def build_truncated_line() -> str:
    """A record whose `text` string opens exactly at char BREAK_AT, then stops.

    The title is padded so the offset lands where the traceback says it did.
    """
    marker = '"text": "'
    for pad in range(0, 8000):
        rec = record("99999999", "Padded title " + ("x" * pad), ABSTRACT * 4)
        line = json.dumps(rec, ensure_ascii=False)
        # Cut mid-sentence, inside the string, with nothing closing it.
        candidate = line[: line.index(marker) + len(marker) + 120]
        try:
            json.loads(candidate)
        except json.JSONDecodeError as exc:
            # Match on the position the decoder actually reports, which is the
            # opening quote rather than the first character of content.
            if exc.pos == BREAK_AT:
                return candidate
    raise SystemExit("could not place the break at char %d" % BREAK_AT)


def main() -> None:
    good = [
        record("32865377", "Cardiovascular and Renal Outcomes with Empagliflozin",
               ABSTRACT),
        record("34449189", "Empagliflozin in Heart Failure with a Preserved Ejection Fraction",
               ABSTRACT),
        record("30990260", "SGLT2 inhibitors and kidney outcomes: a meta-analysis",
               ABSTRACT),
        record("31535829", "Dapagliflozin in Patients with Heart Failure",
               ABSTRACT),
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in good]
    truncated = build_truncated_line()

    # Terminated records, then the interrupted one: no closing quote, no newline.
    OUT.write_text("\n".join(lines) + "\n" + truncated, encoding="utf-8")

    print("wrote %s" % OUT)
    print("  intact records: %d" % len(good))
    print("  truncated final line: %d chars, ends %r" % (len(truncated), truncated[-40:]))
    try:
        json.loads(truncated)
    except json.JSONDecodeError as exc:
        print("  reproduces: %s" % exc)


if __name__ == "__main__":
    main()
