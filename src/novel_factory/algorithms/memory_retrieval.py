"""Source-preserving ranking over an *already authorized* candidate set.

This candidate P3-03 algorithm is deliberately not a MemoryService, permission
checker, or exact-state query. Callers must obtain one fixed, authorized cut and
recheck authorization before delivering results. Missing fuzzy search results
never establish absence. No model, database, event write, or cache lives here.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

METHOD_VERSION = "nf-candidate-memory-rank-1"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def terms(value: str) -> frozenset[str]:
    """Stable Chinese bigrams plus Latin words; not a provider tokenizer."""
    normalized = value.casefold()
    result = set(re.findall(r"[a-z0-9_]+", normalized))
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        result.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
        if len(run) == 1:
            result.add(run)
    return frozenset(result)


@dataclass(frozen=True)
class Passage:
    source_id: str
    text: str
    sha256: str
    entities: tuple[str, ...] = ()
    related_source_ids: tuple[str, ...] = ()
    category: str = ""

    def validate(self) -> None:
        if not self.source_id or not self.text or self.sha256 != sha256_text(self.text):
            raise ValueError("Source text/hash is missing or inconsistent")


@dataclass(frozen=True)
class RankedPassage:
    passage: Passage
    score: float
    reason: str


@dataclass(frozen=True)
class PackedContext:
    passages: tuple[RankedPassage, ...]
    content: str
    content_bytes: int
    omitted_source_ids: tuple[str, ...]
    status: str = "PARTIAL_SEARCH_ONLY"


def rank_authorized(
    passages: Iterable[Passage], query: str, *, entity_ids: Iterable[str] = (),
    expand_links: bool = False, seed_limit: int = 3,
) -> tuple[RankedPassage, ...]:
    """Rank without expanding outside the supplied authorized ID closure.

    Link expansion is one hop, bounded by seed_limit. Hidden source IDs are not
    output or counted. An entity selector gives all methods the same structured
    input; there is no gold-answer parameter or access to benchmark questions.
    """
    if seed_limit < 0:
        raise ValueError("seed_limit must be nonnegative")
    available: dict[str, Passage] = {}
    for passage in passages:
        passage.validate()
        if passage.source_id in available:
            raise ValueError("Duplicate source ID in the authorized cut")
        available[passage.source_id] = passage
    # The returned value objects are deliverable data too. Merely ignoring a
    # hidden link during ranking is insufficient: callers may serialize the
    # complete dataclass, including related_source_ids. Remove that metadata
    # before any RankedPassage is constructed.
    available = {source_id: replace(passage, related_source_ids=tuple(
        linked_id for linked_id in passage.related_source_ids if linked_id in available))
        for source_id, passage in available.items()}
    query_terms, entity_set = terms(query), set(entity_ids)
    ranked: dict[str, RankedPassage] = {}
    for passage in available.values():
        candidate_terms = terms(passage.text)
        shared = len(query_terms & candidate_terms)
        lexical = shared / max(1, len(query_terms))
        entity = len(entity_set.intersection(passage.entities)) / max(1, len(entity_set))
        score = lexical + 0.35 * entity
        if score > 0:
            ranked[passage.source_id] = RankedPassage(passage, score, "lexical_and_entity")
    if expand_links:
        seeds = sorted(ranked.values(), key=lambda r: (-r.score, r.passage.source_id))[:seed_limit]
        for seed in seeds:
            for linked_id in seed.passage.related_source_ids:
                if linked_id not in available:
                    continue
                link_score = seed.score * 0.85
                previous = ranked.get(linked_id)
                if previous is None or previous.score < link_score:
                    ranked[linked_id] = RankedPassage(
                        available[linked_id], link_score, "authorized_source_link")
    return tuple(sorted(ranked.values(), key=lambda r: (-r.score, r.passage.source_id)))


def pack_context(ranked: Iterable[RankedPassage], budget_bytes: int) -> PackedContext:
    """Pack whole anchored passages under the same measured UTF-8 byte budget.

    No partial sentence/source hash is silently substituted. All headers and
    separators count toward budget. Optional omissions remain explicit; this
    function cannot label hard constraints complete or estimate a model bill.
    """
    if budget_bytes < 0:
        raise ValueError("budget_bytes must be nonnegative")
    selected: list[RankedPassage] = []
    omitted: list[str] = []
    blocks: list[str] = []
    used = 0
    seen: set[str] = set()
    for item in ranked:
        passage = item.passage
        passage.validate()
        if passage.source_id in seen:
            raise ValueError("Duplicate source ID in ranked output")
        seen.add(passage.source_id)
        block = f"[{passage.source_id} sha256={passage.sha256}]\n{passage.text}"
        size = len(block.encode("utf-8")) + (2 if blocks else 0)
        if used + size <= budget_bytes:
            selected.append(item)
            blocks.append(block)
            used += size
        else:
            omitted.append(passage.source_id)
    content = "\n\n".join(blocks)
    return PackedContext(tuple(selected), content, used, tuple(omitted))
