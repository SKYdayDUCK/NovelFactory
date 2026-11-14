"""Transport repair and evidence repair have separate local retry allowances."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from novel_factory.application import review_recovery
from novel_factory.domain.errors import DomainError


def recover(history, *, schema_repairs=0):
    changes, events = [], []
    repo = SimpleNamespace(conn=object(), now=lambda: "synthetic-time",
        one=lambda table, *_args, **_kwargs: {
            "supervisor_jobs": {"config": {"required_review_mcp": True}},
            "call_attempts": {"attempt_id": "synthetic-attempt"}}[table],
        change=lambda *args: changes.append(args))
    gateway = SimpleNamespace(_read_audit=lambda *_args: {"payload": {
        "outcome": "VALIDATED_CANDIDATE", "output_ref": {"synthetic": "candidate"},
        "input_ref": {"synthetic": "input"}}})
    kernel = SimpleNamespace(tasks=SimpleNamespace(_emit=lambda *args: events.append(args)))
    task = {"task_id": "synthetic-task", "generation": 1, "state": "RESULT_STORED"}
    node = {"task_id": task["task_id"], "state": "ENROLLED", "evidence_repairs": deepcopy(history),
            "schema_repairs": [{} for _ in range(schema_repairs)]}
    error = DomainError("REVIEW_EVIDENCE_REQUIRED", "A mandatory correction needs a verified original-text span")
    assert review_recovery.recover_review(kernel, repo, gateway, node, task, error)
    return node, changes, events, error


def test_transport_repair_does_not_consume_second_evidence_reread():
    history = [{"family": "NATIVE_REVIEW_TOOL_RECOVERY", "code": "NATIVE_REVIEW_TOOLS_UNUSED"},
               {"code": "REVIEW_EVIDENCE_REQUIRED"}]
    node, changes, events, error = recover(history)
    assert node["state"] == "PENDING" and node["reason"] == "REVIEW_EVIDENCE_REREAD"
    assert len(changes) == len(events) == 1
    assert node["evidence_repairs"][:2] == history
    repair = node["evidence_repairs"][-1]
    assert repair["family"] == review_recovery.FAMILY
    assert repair["diagnostic"] == {"code": error.code, "message": error.safe_message}
    assert error.safe_message in repair["method"]
    assert all(value in repair["method"] for value in ("passages[].span", "quote_sha256", "text_ref"))


@pytest.mark.parametrize("families", [(None, None), (review_recovery.FAMILY, review_recovery.FAMILY),
                                     (None, review_recovery.FAMILY)])
def test_two_evidence_repairs_still_exhaust_the_local_allowance(families):
    history = [{"code": "REVIEW_EVIDENCE_REQUIRED", **({"family": family} if family else {})} for family in families]
    node, changes, events, _ = recover(history)
    assert node["state"] == "STOPPED" and node["reason"] == "REVIEW_EVIDENCE_REPAIR_EXHAUSTED"
    assert node["evidence_repairs"] == history and changes == events == []


def test_separate_families_still_share_total_six_repair_limit():
    history = [{"family": "NATIVE_REVIEW_TOOL_RECOVERY", "code": "NATIVE_REVIEW_TOOLS_UNUSED"},
               {"code": "REVIEW_EVIDENCE_REQUIRED"}]
    node, changes, events, _ = recover(history, schema_repairs=4)
    assert node["state"] == "STOPPED" and node["reason"] == "TECHNICAL_REPAIR_BUDGET_EXHAUSTED"
    assert node["evidence_repairs"] == history and changes == events == []
