"""The caller selects a temporal axis independently of the knowledge plane."""
import json

import pytest
from sqlalchemy import event

from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command, seed_sword_history

from .conftest import p, request, seed, start, uid
from .test_history_queries import query


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


def batch(service, principal, req, operations):
    request = {k: req[k] for k in ("schema_version", "task_id", "generation", "read_contract", "knowledge_cut")}
    request["items"] = [
        {"item_id": name, "subject_time": req["subject_time"], "query": operation, "page_size": 100}
        for name, operation in operations.items()
    ]
    result = service.batch(principal, request)
    assert result["all_items_succeeded"]
    return {item["item_id"]: item["payload"] for item in result["items"]}


def test_plan_dependents_and_mixed_plan_batch(query_env, planning_query):
    _, principal, req, plan_id = planning_query
    service = query_env["query"]
    dependents = {"kind": "PLAN_DEPENDENTS", "subject_ids": req["query"]["subject_ids"], "include_inactive": True}
    result = batch(service, principal, req, {
        "dependents": dependents, "relevant": {**req["query"], "time_axis": "RELEVANT"},
        "source": {**req["query"], "time_axis": "SOURCE"},
    })
    assert [p["entry_id"] for p in result["dependents"]["plans"]] == [plan_id]
    assert result["dependents"]["plans"] == result["relevant"]["entries"]
    assert result["source"]["entries"] == []


def test_belief_intent_uses_its_formation_time(query_env):
    kernel = query_env["kernel"]
    world = load_fixture(kernel)
    principal, wr, ids = world["principal"], world["world_ref"], world["ids"]
    source = kernel.put_artifact(principal, wr["work_id"], b"private journal", "EVIDENCE")
    when = point(6)
    decision = run_command(kernel, principal, wr, "RECORD_COGNITION", {
        "actor_id": ids["protagonist"], "statement": "Return on day 9",
        "record_kind": "PRIVATE_DECISION", "epistemic_mode": "DECIDED",
        "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": source}], "at": when,
    })
    task = create_bound_task(kernel, principal, decision["world_ref"], point(60), [ids["protagonist"]])
    req = query(task, decision["world_ref"], [ids["protagonist"]], ["owner"], 6,
                plane="BELIEF", actor=ids["protagonist"])
    op = {"kind": "TIMELINE", "entry_kinds": ["INTENT"], "subject_ids": [ids["protagonist"]], "include_inactive": True}
    service = query_env["query"]
    axes = ["SOURCE", "TARGET", "VALID", "RELEVANT"]
    answers = batch(service, principal, req, {axis: {**op, "time_axis": axis} for axis in axes})
    assert answers["SOURCE"]["entries"] == answers["RELEVANT"]["entries"]
    [intent] = answers["SOURCE"]["entries"]
    assert intent["entry_kind"] == "INTENT" and intent["source_story_time"]["at"] == when
    assert intent["target_time"] is None and intent["valid_time"] is None
    assert answers["TARGET"]["entries"] == answers["VALID"]["entries"] == []


def test_fact_batches_only_load_event_bodies_for_source_time(query_env, record_property):
    kernel, service = query_env["kernel"], query_env["query"]
    world = seed_sword_history(kernel, load_fixture(kernel))
    principal, wr, sword = world["principal"], world["world_ref"], world["ids"]["sword"]
    task = create_bound_task(kernel, principal, wr, point(60), [sword])
    req = query(task, wr, [sword], ["owner"], 20)
    op = {"kind": "TIMELINE", "subject_ids": [sword], "entry_kinds": ["FACT"], "include_inactive": True}
    counts = {}
    reads = []

    def count_event_reads(conn, cursor, statement, parameters, context, executemany):
        if " FROM nf_events " in " ".join(statement.split()):
            reads.append(1)

    event.listen(query_env["db"].engine, "before_cursor_execute", count_event_reads)
    try:
        for axis in ("TARGET", "VALID", "SOURCE", "RELEVANT"):
            reads.clear()
            answer = service.query(principal, {**req, "query": {**op, "time_axis": axis}})
            counts[axis] = len(reads)
            assert bool(answer["payload"]["entries"]) is (axis in {"SOURCE", "RELEVANT"})
        for label, axes in (("absent", ("TARGET", "VALID")), ("mixed", ("TARGET", "SOURCE"))):
            reads.clear()
            result = batch(service, principal, req, {axis: {**op, "time_axis": axis} for axis in axes})
            counts[label] = len(reads)
            assert not result["TARGET"]["entries"]
            assert bool(result[axes[1]]["entries"]) is (axes[1] == "SOURCE")
    finally:
        event.remove(query_env["db"].engine, "before_cursor_execute", count_event_reads)
    record_property("nf_events_selects", json.dumps(counts))
    assert counts == {"TARGET": 0, "VALID": 0, "SOURCE": 1, "RELEVANT": 1, "absent": 0, "mixed": 1}
