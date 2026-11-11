"""Read effects include edges removed by projection and process frontier completion."""

import pytest

from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import (
    create_bound_task,
    entity_value,
    fixture_definition,
    point,
    prepare_command,
    run_command,
)

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize(
    "case", ["implicit_edge_closure", "scoped_timeline", "visited_graph_frontier"]
)
def test_query_projection_change_blocks_disjoint_candidate(kernel_env, case):
    k = kernel_env["kernel"]
    f = fixture_definition("white_bridge")
    ids = f["ids"]
    f["genesis"]["relations"].append(
        {
            "subject_id": ids["north"],
            "predicate": "blocked_by",
            "object_value": entity_value(ids["sword"]),
            "valid_time": {"start": point(1), "end": None, "bounds": "[)"},
        }
    )
    if case == "scoped_timeline":
        f["genesis"]["entities"][ids["guard1"]]["scope_id"] = ids["north"]
    if case == "visited_graph_frontier":
        for who in ["north", "smith"]:
            f["frontiers"][ids[who]] = point(12)
    created = k.bootstrap_work("projection-change", f["genesis"], frontiers=f["frontiers"])
    wr, admin = created["world_ref"], created["principal"]
    task = create_bound_task(k, admin, wr, point(20), [ids["guard2"]], ["lifecycle"])
    query = (
        {
            "kind": "TIMELINE",
            "subject_ids": [ids["guard1"]],
            "entry_kinds": ["FACT"],
            "time_axis": "SOURCE",
            "include_inactive": False,
        }
        if case == "scoped_timeline"
        else {
            "kind": "NEIGHBORHOOD",
            "seed_ids": [ids["north"]],
            "predicates": ["blocked_by", "possessed_by"],
            "max_hops": 3,
            "max_edges": 30,
        }
    )

    def lookup(task, world):
        return WorldQueryService(k).query(
            admin,
            {
                "schema_version": "2.1",
                "task_id": task["task_id"],
                "generation": task["generation"],
                "read_contract": {
                    "revision": {"mode": "FIXED", "world_ref": world},
                    "process_policy": "ALLOW_PARTIAL",
                    "index_policy": "AUTHORITATIVE",
                    "max_wait_ms": 0,
                },
                "knowledge_cut": {"plane": "WORLD"},
                "subject_time": {"kind": "POINT", "at": point(10)},
                "query": query,
                "page_size": 30,
                "cursor": None,
            },
        )["payload"]

    prior = lookup(task, wr)
    cmd = prepare_command(
        k,
        admin,
        wr,
        "RESOURCE_TRANSFER",
        {
            "from_account": ids["warehouse"],
            "to_account": ids["relief"],
            "resource_id": ids["grain"],
            "quantity": "1",
            "unit": "bag",
            "at": point(20),
        },
        mode="DEPENDENCY_GUARDED",
        task=task,
    )
    if case == "implicit_edge_closure":
        changed = run_command(
            k, admin, wr, "DESTROY_ITEM", {"item_id": ids["sword"], "at": point(5)}
        )
    else:
        scope = ids["north" if case == "scoped_timeline" else "sword"]
        changed = run_command(
            k, admin, wr, "ADVANCE_SCOPE", {"scope_id": scope, "target": point(11)}
        )
    new_world = changed["world_ref"]
    current_task = create_bound_task(k, admin, new_world, point(20), [ids["guard2"]], ["lifecycle"])
    current = lookup(current_task, new_world)
    if case == "implicit_edge_closure":
        assert len(prior["relations"]) == 2 and len(current["relations"]) == 1
    else:
        assert prior["coverage"] == "PARTIAL" and current["coverage"] == "COMPLETE"
    with pytest.raises(DomainError) as blocked:
        k.accept(admin, cmd["request"])
    assert blocked.value.code == "DEPENDENCY_CONFLICT"


@pytest.mark.parametrize("source_kind", ["existing_text", "spaced_version_reference"])
def test_authorized_message_read_fences_all_supported_source_bindings(kernel_env, source_kind):
    import json
    from novel_factory.application.message_sources import MessageSourceService
    from novel_factory.domain.canonical import canonical_bytes
    from test_s9_dependencies import set_fact, world

    k = kernel_env["kernel"]
    f = world(k)
    wr, admin, carrier, ids = f["world_ref"], f["principal"], f["letter"], f["ids"]
    body = "星落时开门。"
    if source_kind == "existing_text":
        ref = k.put_artifact(admin, wr["work_id"], body.encode(), "TEXT")
        encoded_ref = canonical_bytes(ref).decode()
    else:
        ref = MessageSourceService(k).save_version(admin, wr, body, "original")["source_ref"]
        encoded_ref = json.dumps(ref, indent=2)
    named = set_fact(
        k, f, wr, carrier, "message_content_ref", {"kind": "STRING", "value": encoded_ref}, 2
    )
    task = create_bound_task(k, admin, named, point(20), [ids["guard2"]], ["lifecycle"])
    compiled = task["compiled"]["compiled"]["bundle"]
    assert (
        k.get_artifact(
            admin,
            wr["work_id"],
            ref,
            task_id=task["task_id"],
            generation=task["generation"],
            context_bundle_id=compiled["context_bundle_id"],
            world_ref=named,
            story_time=point(20),
            knowledge_cut={"plane": "WORLD"},
        ).decode()
        == body
    )
    cmd = prepare_command(
        k,
        admin,
        named,
        "RESOURCE_TRANSFER",
        {
            "from_account": ids["warehouse"],
            "to_account": ids["relief"],
            "resource_id": ids["grain"],
            "quantity": "1",
            "unit": "bag",
            "at": point(20),
        },
        mode="DEPENDENCY_GUARDED",
        task=task,
    )
    set_fact(k, f, named, carrier, "message_authentic", {"kind": "BOOLEAN", "value": False}, 5)
    with pytest.raises(DomainError) as blocked:
        k.accept(admin, cmd["request"])
    assert blocked.value.code == "DEPENDENCY_CONFLICT"
