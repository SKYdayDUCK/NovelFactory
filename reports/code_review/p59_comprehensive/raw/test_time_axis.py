"""The caller selects a temporal axis independently of the knowledge plane."""
import pytest

from .conftest import p, request, seed, start, uid


@pytest.fixture
def planning_query(query_env):
    world = seed(query_env)
    task = start(query_env, world)
    kernel = query_env["kernel"]
    work = world["world_ref"]["work_id"]
    subject = world["ids"]["sword"]
    draft = kernel.put_artifact(world["principal"], work, b"return the sword", "TEXT")
    plan = {
        "plan_id": uid(), "plan_ref": draft, "status": "APPROVED",
        "target_time": {"kind": "EXACT", "at": p(50)}, "subject_ids": [subject],
        "summary": "Return the sword", "requires_active": [subject], "fulfillment_event_refs": [],
    }
    manifest = kernel.put_artifact(world["principal"], work, {"schema_version": "p1", "plans": [plan]},
                                   "PLAN_MANIFEST")
    req = request(task, at=p(50), operation={"kind": "TIMELINE", "subject_ids": [subject],
                  "entry_kinds": ["PLAN"], "time_axis": "TARGET", "include_inactive": True})
    req["knowledge_cut"] = {"plane": "PLAN", "plan_manifest_ref": manifest}
    return world, task["principal"], req, plan["plan_id"]


@pytest.mark.parametrize("axis,expected", [("SOURCE", False), ("VALID", False),
                                           ("TARGET", True), ("RELEVANT", True)])
def test_plan_filters_by_requested_time_axis(query_env, planning_query, axis, expected):
    _, principal, req, plan_id = planning_query
    req["query"]["time_axis"] = axis
    result = query_env["query"].query(principal, req)
    assert [entry["entry_id"] for entry in result["payload"]["entries"]] == ([plan_id] if expected else [])


@pytest.mark.parametrize("axis,expected", [("SOURCE", False), ("TARGET", False),
                                           ("VALID", True), ("RELEVANT", True)])
def test_assertion_filters_by_requested_time_axis(query_env, axis, expected):
    world = seed(query_env)
    task = start(query_env, world)
    req = request(task, operation={"kind": "TIMELINE", "subject_ids": [world["ids"]["sword"]],
                  "entry_kinds": ["STATE_ASSERTION"], "time_axis": axis, "include_inactive": True})
    entries = query_env["query"].query(task["principal"], req)["payload"]["entries"]
    assert bool(entries) is expected
