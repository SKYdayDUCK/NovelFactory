"""Deterministic, original-source-anchored extractive context representations.

Input is an already authorized fixed cut. This module cannot grant access,
resolve latest, infer facts, or establish that omitted evidence does not exist.
A summary is a set of literal original ranges; it never pretends to be full text.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from novel_factory.algorithms.memory_retrieval import Passage, rank_authorized, sha256_text

REPRESENTATION_VERSION = "nf-source-extract-1"
STRATEGIES = ("full", "lead", "lead_tail")


@dataclass(frozen=True)
class SourceExtract:
    source_id: str
    original_sha256: str
    original_code_points: int
    spans: tuple[tuple[int, int], ...]
    text: str
    extract_sha256: str

    @property
    def original_complete(self) -> bool:
        return self.spans == ((0, self.original_code_points),)


@dataclass(frozen=True)
class EvidenceContext:
    extracts: tuple[SourceExtract, ...]
    content: str
    content_bytes: int
    candidate_source_ids: tuple[str, ...]
    ranking_trace: tuple[tuple[str, float, str], ...]
    omitted_source_ids: tuple[str, ...]
    status: str = "PARTIAL_SEARCH_ONLY"


def sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Keep delimiters and whitespace in exact Unicode-code-point ranges."""
    boundaries = [match.end() for match in re.finditer(r"[。！？!?](?:[”’\"])?", text)]
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))
    result, start = [], 0
    for end in boundaries:
        if end > start:
            result.append((start, end))
        start = end
    return tuple(result)


def extract_source(passage: Passage, strategy: str) -> SourceExtract:
    passage.validate()
    if strategy not in STRATEGIES:
        raise ValueError("Unknown source representation")
    sentences = sentence_spans(passage.text)
    if strategy == "full":
        spans = ((0, len(passage.text)),)
    elif strategy == "lead":
        spans = sentences[:1]
    else:
        spans = sentences[:1] + (sentences[-1:] if len(sentences) > 1 else ())
    # Adjacent retained ranges are merged so completeness has an exact meaning.
    merged = []
    for start, end in spans:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    spans = tuple(merged)
    text = "\n[…]\n".join(passage.text[start:end] for start, end in spans)
    return SourceExtract(passage.source_id, passage.sha256, len(passage.text),
                         spans, text, sha256_text(text))


def verify_extract(original: Passage, extract: SourceExtract) -> bool:
    original.validate()
    if (extract.source_id != original.source_id or extract.original_sha256 != original.sha256
            or extract.original_code_points != len(original.text) or not extract.spans):
        return False
    previous = -1
    for start, end in extract.spans:
        if not (type(start) is int and type(end) is int
                and 0 <= start < end <= len(original.text) and start > previous):
            return False
        previous = end
    expected = "\n[…]\n".join(original.text[start:end] for start, end in extract.spans)
    return extract.text == expected and extract.extract_sha256 == sha256_text(expected)


def compile_evidence_context(passages: Iterable[Passage], query: str, *, budget_bytes: int,
                             strategy: str = "full", entity_ids: Iterable[str] = (),
                             expand_links: bool = False) -> EvidenceContext:
    """Compare representations on identical candidate IDs and measured budgets.

    Citation metadata, separators, and omission markers all consume UTF-8 bytes.
    Source IDs and hashes survive summarization. This is not a provider-token
    count, model-generated summary, complete fact query, or permission service.
    """
    if type(budget_bytes) is not int or budget_bytes < 0:
        raise ValueError("Budget must be a nonnegative integer")
    originals = list(passages)
    ids = [passage.source_id for passage in originals]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate source ID in authorized cut")
    extracts = {passage.source_id: extract_source(passage, strategy) for passage in originals}
    represented = [Passage(p.source_id, extracts[p.source_id].text,
                          extracts[p.source_id].extract_sha256, p.entities,
                          p.related_source_ids, p.category) for p in originals]
    ranked = rank_authorized(represented, query, entity_ids=entity_ids, expand_links=expand_links)
    blocks, selected, omitted, trace = [], [], [], []
    used = 0
    for row in ranked:
        item = extracts[row.passage.source_id]
        ranges = ",".join(f"{start}:{end}" for start, end in item.spans)
        block = (f"[{item.source_id} original_sha256={item.original_sha256} "
                 f"ranges={ranges} unit=unicode_code_point complete={str(item.original_complete).lower()}]\n"
                 + item.text)
        size = len(block.encode("utf-8")) + (2 if blocks else 0)
        if used + size <= budget_bytes:
            blocks.append(block)
            selected.append(item)
            used += size
            trace.append((item.source_id, row.score, row.reason))
        else:
            omitted.append(item.source_id)
    return EvidenceContext(tuple(selected), "\n\n".join(blocks), used, tuple(sorted(ids)),
                           tuple(trace), tuple(omitted))
