"""Continuity boundaries that must fail before any storage mutation."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.continuity import validate_append, validate_entries
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point

pytestmark = pytest.mark.unit


def minimal():
    return {"entry_id": str(uuid4()), "thread_id": str(uuid4()), "volume_ordinal": 1,
        "kind": "READER_DISCLOSURE", "plane": "READER", "status": "OPEN", "entity_ids": [],
        "story_interval": {"start": point(1), "end": point(2), "bounds": "[)"},
        "interpretation": "旁白提到钥匙完好；不能直接改写世界状态。",
        "epistemic_status": "TEXT_ASSERTION_NOT_WORLD_FACT", "source_events": [],
        "source_texts": [{"retained": "domain-only-test"}], "supersedes_entry_ids": [], "planned_payoff_at": None}


def test_reader_disclosure_cannot_be_promoted_into_objective_evidence():
    item = minimal()
    assert validate_entries([item], 1)[item["thread_id"]] == item["entry_id"]
    item["epistemic_status"] = "EVENT_REFERENCE"
    with pytest.raises(DomainError) as error:
        validate_entries([item], 1)
    assert error.value.code == "INVALID_EVIDENCE"


def test_evolution_requires_same_thread_plane_and_explicit_predecessor():
    first = minimal()
    second = {**deepcopy(first), "entry_id": str(uuid4()), "volume_ordinal": 2}
    with pytest.raises(DomainError) as skipped:
        validate_entries([first, second], 2)
    assert skipped.value.code == "CONTINUITY_THREAD_CONFLICT"
    second["supersedes_entry_ids"] = [first["entry_id"]]
    assert validate_entries([first, second], 2)[first["thread_id"]] == second["entry_id"]
    second["thread_id"] = str(uuid4())
    with pytest.raises(DomainError):
        validate_entries([first, second], 2)


def test_revision_preserves_old_entry_even_when_new_edition_supersedes_it():
    first = minimal()
    prior = {"request": {"entries": [first], "ordered_release_refs": ["one"], "world_ref": "old"}}
    replacement = {"entries": [], "ordered_release_refs": ["two"], "world_ref": "new",
                   "operation": "REVISE", "revision_reason": "改版"}
    with pytest.raises(DomainError) as dropped:
        validate_append(prior, replacement)
    assert dropped.value.code == "CONTINUITY_HISTORY_LOSS"


def test_contract_rejects_opaque_unbounded_extra_fields():
    with pytest.raises(DomainError) as invalid:
        validate_contract("ContinuityPublishRequest", {"schema_version": "2.3", "candidate_ref": {},
            "review_ref": {}, "idempotency_key": "x", "skip_review": True}, "continuity")
    assert invalid.value.code == "INVALID_ARGUMENT"
