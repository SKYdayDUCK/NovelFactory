"""Independent PG17 checks of change tokens; no model is invoked."""

import pytest

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.lifecycle_state import project_change_token
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.supervisor_wake import wait_for_change, wake_listener

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_change_token_ignores_observation_churn_but_tracks_selected_content(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, principal = world["world_ref"], world["principal"]
    args = kernel, principal, wr["work_id"], wr["branch_id"]
    first = project_change_token(*args)
    payload = {"world_ref": wr, "value": "first"}
    ref = kernel.put_artifact(principal, wr["work_id"], payload, "EVIDENCE")
    assert project_change_token(*args) == first
    key = {"work_id": wr["work_id"], "selection_kind": "LIFECYCLE_TEST:" + wr["branch_id"]}
    with kernel.db.uow() as repo:
        repo.add("selections", {**key, "artifact_ref": ref, "epoch": 1})
    selected = project_change_token(*args)
    assert selected != first
    same_bytes_new_id = kernel.put_artifact(principal, wr["work_id"], payload, "EVIDENCE")
    with kernel.db.uow() as repo:
        repo.change("selections", key, {"artifact_ref": same_bytes_new_id, "epoch": 2})
    assert project_change_token(*args) == selected
    changed = kernel.put_artifact(principal, wr["work_id"], {**payload, "value": "changed"}, "EVIDENCE")
    with kernel.db.uow() as repo:
        repo.change("selections", key, {"artifact_ref": changed, "epoch": 3})
    assert project_change_token(*args) != selected


def test_change_token_checks_current_authority(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    principal, wr = world["principal"], world["world_ref"]
    with kernel.db.uow() as repo:
        repo.change("grants", {"grant_id": principal.grant_id}, {"active": False})
    with pytest.raises(DomainError) as denied:
        project_change_token(kernel, principal, wr["work_id"], wr["branch_id"])
    assert denied.value.code == "VIEW_DENIED"


def test_notification_burst_is_coalesced_without_repeated_scans(kernel_env):
    db = kernel_env["kernel"].db
    with wake_listener(db.engine) as listener:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("SELECT pg_notify('nf_supervisor_wake', n::text) FROM generate_series(1,12) n")
        assert wait_for_change(listener, 2)
        assert not wait_for_change(listener, 0)


def test_token_allowance_change_wakes_without_changing_money_budget(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    principal, wr = world["principal"], world["world_ref"]
    args = kernel, principal, wr["work_id"], wr["branch_id"]
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    with kernel.db.uow(write=False) as repo:
        money_before = repo.one("budgets", {"budget_id": budget})
    initial = project_change_token(*args)
    service = SharedBudgetService(kernel)
    service.set_limit(principal, wr["work_id"], wr["branch_id"], budget, 1000)
    limited = project_change_token(*args)
    assert limited != initial
    service.set_limit(principal, wr["work_id"], wr["branch_id"], budget, 2000, expected_total_tokens=1000)
    assert project_change_token(*args) != limited
    with kernel.db.uow(write=False) as repo:
        assert repo.one("budgets", {"budget_id": budget}) == money_before
