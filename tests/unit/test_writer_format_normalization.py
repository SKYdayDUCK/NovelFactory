"""A single frozen metadata quote is repairable; prose and other syntax are not."""
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.application.writer_format_normalization import derive
from novel_factory.domain.canonical import strict_json_loads
from novel_factory.domain.errors import DomainError

pytestmark = pytest.mark.unit
CONTRACT = {"name": "AgentTextOutput", "version": "agent"}


def example():
    identity = {"task_id": str(uuid4()), "generation": 1, "context_bundle_id": str(uuid4()),
        "world_ref": {key: str(uuid4()) for key in ("work_id", "branch_id", "world_revision_id")},
        "role_id": "writer", "actor_id": None}
    envelope = {"schema_version": "2.1", **identity, "status": "SUCCEEDED", "unresolved_questions": [],
        "payload": {"text": '工程正文含引号“甲”和ASCII "quoted"，以及换行\n与斜杠\\。',
                    "event_refs": [], "important_deltas": ["工程声明保持原样"], "evidence_refs": []}}
    return identity, envelope


def rendered(envelope):
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def broken(envelope, field="work_id"):
    token = '"' + field + '":"' + envelope["world_ref"][field] + '"'
    return rendered(envelope).replace(token, token[:-1], 1)


@pytest.mark.parametrize("field", ["work_id", "branch_id", "world_revision_id"])
def test_unique_closing_quote_restores_only_the_exact_frozen_metadata(field):
    identity, envelope = example()
    raw = broken(envelope, field)
    fixed, proof = derive(raw, identity, CONTRACT)
    assert fixed == envelope and proof["world_ref_field"] == field
    assert raw[:proof["offset_codepoints"]] + '"' + raw[proof["offset_codepoints"]:] == rendered(envelope)
    assert proof["original_payload_slice_sha256"] == proof["normalized_payload_slice_sha256"]
    assert strict_json_loads(rendered(fixed))["payload"] == envelope["payload"]


@pytest.mark.parametrize("change", ["valid", "foreign_identity", "duplicate", "two_quotes", "body_syntax",
    "missing_comma", "extra_world_key", "truncated", "nonwriter", "other_contract", "questions", "payload_schema"])
def test_metadata_derivation_rejects_every_other_failure(change):
    identity, envelope = example()
    raw, contract = broken(envelope), CONTRACT
    if change == "valid":
        raw = rendered(envelope)
    elif change == "foreign_identity":
        identity = deepcopy(identity)
        identity["world_ref"]["branch_id"] = str(uuid4())
    elif change == "duplicate":
        raw = raw.replace('"generation":1', '"generation":1,"generation":1')
    elif change == "two_quotes":
        token = '"branch_id":"' + envelope["world_ref"]["branch_id"] + '"'
        raw = raw.replace(token, token[:-1])
    elif change == "body_syntax":
        raw = raw.replace('"text":', '"text"', 1)
    elif change == "missing_comma":
        raw = raw.replace(',"world_revision_id"', '"world_revision_id"')
    elif change == "extra_world_key":
        envelope["world_ref"]["another"] = str(uuid4())
        raw = broken(envelope)
    elif change == "truncated":
        raw = raw[:-1]
    elif change == "nonwriter":
        identity = {**identity, "role_id": "reviewer"}
    elif change == "other_contract":
        contract = {"name": "AgentReviewOutput", "version": "agent"}
    elif change == "questions":
        envelope["unresolved_questions"] = ["Engineering unresolved prerequisite"]
        raw = broken(envelope)
    else:
        del envelope["payload"]["important_deltas"]
        raw = broken(envelope)
    with pytest.raises(DomainError):
        derive(raw, identity, contract)
