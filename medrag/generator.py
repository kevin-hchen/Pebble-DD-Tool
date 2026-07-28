"""Grounded answer generation with enforced inline citations.

The system prompt is the safety surface of this project: the model may only use
the supplied context, must cite every claim by source marker, and must say so
when the retrieved literature does not answer the question. If no API key is
available, an extractive fallback returns the retrieved evidence verbatim so the
pipeline still produces a cited, inspectable answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config
from .documents import Retrieved
from .providers import make_client

# Bracketed citation clusters written by real models: single [4], range [1-6] or
# en-dash [1–6], comma list [12, 14, 17], and mixes like [1-3, 7]. Anything
# non-numeric inside the brackets fails the match, so "[missing]" is not a
# citation.
_CITE_CLUSTER_RE = re.compile(r"\[([\d\s,\-–]+)\]")
_MAX_CITATION_SPAN = 50


def parse_citation_indices(text: str) -> set[int]:
    """Expand every bracketed citation cluster to the set of indices it names.

    [1-6] -> {1,2,3,4,5,6}. Ranges wider than _MAX_CITATION_SPAN are skipped
    outright so a malformed [1-9999] cannot allocate a giant set. Reversed or
    non-integer ranges are skipped rather than silently reordered.
    """
    out: set[int] = set()
    for inner in _CITE_CLUSTER_RE.findall(text):
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
            if m:
                lo, hi = int(m.group(1)), int(m.group(2))
                if 0 < lo <= hi and hi - lo + 1 <= _MAX_CITATION_SPAN:
                    out.update(range(lo, hi + 1))
                continue
            if part.isdigit():
                out.add(int(part))
    return out

SYSTEM_PROMPT = """You are MedRAG, a clinical literature assistant. You answer \
questions strictly from the retrieved excerpts supplied to you.

Rules:
1. Use ONLY the numbered context excerpts. Never rely on prior knowledge, and \
never fill gaps with plausible-sounding detail.
2. Cite every factual claim inline with its source marker, e.g. [1] or [2][3]. \
A sentence carrying a number, effect size, or clinical claim must carry a citation.
3. If the excerpts do not contain the answer, say so plainly and state what is \
missing. Do not speculate. Partial evidence should be reported as partial.
4. Preserve quantitative detail exactly as written: effect sizes, confidence \
intervals, p-values, doses, sample sizes, and follow-up duration.
5. Note disagreement between sources rather than averaging them, and flag \
limitations the excerpts themselves report (small n, observational design, \
short follow-up, industry funding).
6. Be concise and clinical in register. Lead with the direct answer, then the \
supporting evidence.
7. This is a literature summarization tool, not medical advice. Do not issue \
individualized treatment recommendations."""

USER_TEMPLATE = """Question: {question}

Retrieved excerpts:
{context}

Answer the question using only these excerpts, with inline [n] citations."""


@dataclass
class Answer:
    text: str
    sources: list[Retrieved] = field(default_factory=list)
    model: str = ""
    grounded: bool = True
    usage: dict = field(default_factory=dict)

    @property
    def cited_indices(self) -> set[int]:
        return parse_citation_indices(self.text)

    def bibliography(self) -> str:
        lines = []
        for i, r in enumerate(self.sources, 1):
            tag = f"PMID {r.chunk.doc_id}" if r.chunk.doc_id.isdigit() else r.chunk.doc_id
            lines.append(f"[{i}] {r.chunk.title} — {r.chunk.citation} ({tag}) {r.chunk.url}".rstrip())
        return "\n".join(lines)


def build_context(retrieved: list[Retrieved], max_chars: int) -> str:
    """Render numbered excerpts, truncating at the context budget."""
    blocks, total = [], 0
    for i, r in enumerate(retrieved, 1):
        tag = f"PMID {r.chunk.doc_id}" if r.chunk.doc_id.isdigit() else r.chunk.doc_id
        block = f"[{i}] ({tag} — {r.chunk.citation})\n{r.chunk.text}"
        if total + len(block) > max_chars:
            # Never return an empty context: if even the top passage overflows the
            # budget, truncate it rather than dropping the evidence entirely.
            if not blocks:
                blocks.append(block[:max_chars].rstrip() + " […]")
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def _extractive_answer(question: str, retrieved: list[Retrieved]) -> str:
    """No-API fallback: return the top evidence verbatim, cited, clearly labelled."""
    lines = [
        "*(No LLM API key configured — showing retrieved evidence verbatim instead of a "
        "generated synthesis.)*",
        "",
        f"Top passages for: {question}",
        "",
    ]
    for i, r in enumerate(retrieved, 1):
        body = r.chunk.text.split("\n", 1)[-1].strip()
        lines.append(f"[{i}] (cosine {r.score:.3f}) {body[:600]}{'…' if len(body) > 600 else ''}")
        lines.append("")
    return "\n".join(lines).strip()


class Generator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if getattr(cfg, "offline", False):
            print("[medrag] offline mode: no text will leave this machine")
            self.client = None
        else:
            self.client = make_client(cfg)

    def generate(self, question: str, retrieved: list[Retrieved]) -> Answer:
        if not retrieved:
            return Answer(
                text=(
                    "No relevant passages were retrieved for this question. The indexed "
                    "corpus likely does not cover this topic — try ingesting more "
                    "literature or rephrasing the query."
                ),
                sources=[],
                model=self.cfg.chat_model if self.client else "extractive",
                grounded=False,
            )

        if self.client is None:
            return Answer(
                text=_extractive_answer(question, retrieved),
                sources=retrieved,
                model="extractive-fallback",
            )

        context = build_context(retrieved, self.cfg.max_context_chars)
        resp = self.client.chat.completions.create(
            model=self.cfg.chat_model,
            temperature=self.cfg.temperature,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(question=question, context=context)},
            ],
        )
        usage = getattr(resp, "usage", None)
        return Answer(
            text=resp.choices[0].message.content.strip(),
            sources=retrieved,
            model=self.cfg.chat_model,
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
            },
        )
