"""Only completed immutable sources may outlive an execution window."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def qualification(*, state="ACCEPTED", expired=True, **overrides):
    now = datetime.now(UTC)
    task = {"task_id": "task", "principal_id": "reader", "grant_id": "grant", "generation": 1,
            "state": state, "deadline": now + timedelta(seconds=-1 if expired else 60),
            "lease_expiry": now - timedelta(seconds=1)}
    task.update(overrides)
    spec = {"policy_version": 1}
    row = {"spec": spec, "input_hash": fingerprint(spec)}
    repo = SimpleNamespace(now=lambda: now, one=lambda *_args, **_kwargs: row)
    principal = SimpleNamespace(principal_id="reader", grant_id="grant")
    return WorldQueryService(None), repo, principal, task, row


def test_expired_accepted_source_is_internal_only():
    query, repo, principal, task, _ = qualification()
    assert query._qualify_task(repo, principal, task, 1, {}, work={"policy_version": 1},
                               historical_accepted=True)[0] is task
    with pytest.raises(DomainError, match="deadline") as error:
        query._qualify_task(repo, principal, task, 1, {}, work={"policy_version": 1})
    assert error.value.code == "DEADLINE_EXCEEDED"


@pytest.mark.parametrize("state", ["READY", "RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW"])
def test_history_flag_does_not_extend_unaccepted_execution(state):
    query, repo, principal, task, _ = qualification(state=state)
    with pytest.raises(DomainError) as error:
        query._qualify_task(repo, principal, task, 1, {}, work={"policy_version": 1}, historical_accepted=True)
    assert error.value.code == "DEADLINE_EXCEEDED"


def test_history_flag_does_not_extend_running_lease():
    query, repo, principal, task, _ = qualification(state="RUNNING", expired=False)
    with pytest.raises(DomainError) as error:
        query._qualify_task(repo, principal, task, 1, {}, work={"policy_version": 1}, historical_accepted=True)
    assert error.value.code == "LEASE_EXPIRED"


@pytest.mark.parametrize("changed,code", [("principal", "VIEW_DENIED"), ("generation", "STALE_GENERATION"),
                                         ("policy", "TASK_REBIND_REQUIRED"), ("hash", "TASK_NOT_ACCEPTABLE")])
def test_historical_source_retains_identity_policy_and_integrity(changed, code):
    query, repo, principal, task, row = qualification()
    work = {"policy_version": 2 if changed == "policy" else 1}
    if changed == "principal":
        task["principal_id"] = "other"
    if changed == "generation":
        task["generation"] = 2
    if changed == "hash":
        row["input_hash"] = "0" * 64
    with pytest.raises(DomainError) as error:
        query._qualify_task(repo, principal, task, 1, {}, work=work, historical_accepted=True)
    assert error.value.code == code
