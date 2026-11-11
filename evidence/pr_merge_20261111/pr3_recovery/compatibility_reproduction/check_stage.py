"""Isolated historical-source compatibility probe; synthetic test data only."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import hashlib
import inspect
import json
import os
from pathlib import Path
import socket
import threading
import time

import httpx
from sqlalchemy import text
import uvicorn

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, seed_sword_history, create_bound_task, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import database_snapshot
from novel_factory.interfaces.http import create_app


def normalized(value):
    result = json.loads(json.dumps(value, default=str))
    if isinstance(result, dict) and "read_cut" in result:
        result.pop("request_id", None)
        result["read_cut"].pop("read_cut_id", None)
        result["read_cut"].pop("resolved_at", None)
    return result


def query(task, world_ref, subject, at, *, actor=None):
    cut = {"plane": "WORLD"} if actor is None else {
        "plane": "BELIEF", "actor_id": actor, "known_at": point(at)}
    return {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": world_ref},
            "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": point(at)},
        "query": {"kind": "STATE", "subject_ids": [subject], "predicates": ["owner", "active_holder"]},
        "page_size": 100, "cursor": None}


def write(path, value, *, private=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, default=str)
        out.write("\n")


def denial(service, principal, request):
    try:
        service.query(principal, request)
    except DomainError as exc:
        return exc.code
    raise AssertionError("Restricted historical identity unexpectedly received forbidden data")


def seed(kernel, output):
    initial = load_fixture(kernel, "r1_sword")
    world = seed_sword_history(kernel, initial)
    principal, wr, ids = world["principal"], world["world_ref"], world["ids"]
    latest_task = create_bound_task(kernel, principal, wr, point(60), [ids["sword"]])
    initial_task = create_bound_task(kernel, principal, initial["world_ref"], point(5), [ids["sword"]])
    actor = kernel.issue_grant(principal, wr["work_id"], role_id="character_actor",
        actor_id=ids["protagonist"], max_story_time=point(35),
        capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"], planes=["BELIEF"])
    actor_task = create_bound_task(kernel, actor["principal"], wr, point(35), [ids["sword"]],
        ["owner", "active_holder"], plane="BELIEF", actor_id=ids["protagonist"])
    service = WorldQueryService(kernel)
    queries = [{"identity": "admin", "request": query(latest_task, wr, ids["sword"], day)}
               for day in (5, 15, 25, 35, 41)]
    queries += [{"identity": "admin", "request": query(initial_task, initial["world_ref"], ids["sword"], 5)},
                {"identity": "actor", "request": query(actor_task, wr, ids["sword"], 35, actor=ids["protagonist"])}]
    principals = {"admin": principal, "actor": actor["principal"]}
    for item in queries:
        item["expected"] = normalized(service.query(principals[item["identity"]], item["request"]))
    forbidden_world = query(actor_task, wr, ids["sword"], 35)
    forbidden_actor = query(actor_task, wr, ids["sword"], 35, actor=ids["rival"])
    forbidden_future = query(actor_task, wr, ids["sword"], 36, actor=ids["protagonist"])
    denials = [{"request": request, "expected_code": denial(service, actor["principal"], request)}
               for request in (forbidden_world, forbidden_actor, forbidden_future)]
    task_samples = [("admin", latest_task), ("admin", initial_task), ("actor", actor_task)]
    task_samples += [("admin", step["task"]) for step in world["steps"]]
    tasks = [{"identity": identity, "task_id": task["task_id"],
              "expected": normalized(kernel.get_task(principals[identity], task["task_id"]))}
             for identity, task in task_samples]
    with kernel.db.uow(write=False) as repo:
        revisions = normalized(repo.many("revisions", {"work_id": wr["work_id"]}, order=["branch_seq"]))
    with kernel.db.engine.connect() as conn:
        snapshot = database_snapshot(conn)
        migration = conn.scalar(text("SELECT version_num FROM alembic_version"))
    private = {"initial_world_ref": initial["world_ref"], "world_ref": wr,
        "principals": {key: asdict(value) for key, value in principals.items()},
        "tokens": {"admin": initial["token"], "actor": actor["token"]},
        "queries": queries, "denials": denials, "tasks": tasks, "revisions": revisions,
        "receipts": [{"request": step["request"], "receipt": step["receipt"]} for step in world["steps"]],
        "snapshot": snapshot, "objects": kernel.objects.inventory()}
    write(output / "private-state.json", private, private=True)
    return {"status": "PASS", "schema": migration, "original_tables": len(snapshot),
        "formal_commands": len(world["steps"]), "history_revisions": len(revisions),
        "query_cases": len(queries), "permission_denials": len(denials), "task_samples": len(tasks),
        "object_count": len(private["objects"]), "database_snapshot": snapshot,
        "fixture": "r1_sword actual P1 bootstrap and six formal accepts; no direct seed SQL"}


def verify(kernel, output):
    state = json.loads((output / "private-state.json").read_text())
    principals = {key: Principal(**value) for key, value in state["principals"].items()}
    with kernel.db.engine.connect() as conn:
        before = database_snapshot(conn)
        migration = conn.scalar(text("SELECT version_num FROM alembic_version"))
    for name, expected in state["snapshot"].items():
        if name != "alembic_version":
            assert before[name] == expected, f"Historical table changed: {name}"
    added = set(before) - set(state["snapshot"])
    assert added == {"nf_simulation_interactions", "nf_simulation_intents", "nf_simulation_outcomes"}, added
    assert all(before[name]["rows"] == 0 for name in added)
    assert migration == "0003"
    for identity, token in state["tokens"].items():
        assert kernel.authenticate(token) == principals[identity]
    for item in state["receipts"]:
        assert normalized(kernel.accept(principals["admin"], item["request"])) == item["receipt"]
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, state["world_ref"]["work_id"]) == state["world_ref"]
        revisions = normalized(repo.many("revisions", {"work_id": state["world_ref"]["work_id"]}, order=["branch_seq"]))
        assert revisions == state["revisions"]
    service = WorldQueryService(kernel)
    for item in state["queries"]:
        assert normalized(service.query(principals[item["identity"]], item["request"])) == item["expected"]
    for item in state["denials"]:
        assert denial(service, principals["actor"], item["request"]) == item["expected_code"]
    for item in state["tasks"]:
        assert normalized(kernel.get_task(principals[item["identity"]], item["task_id"])) == item["expected"]
    for digest, size in state["objects"].items():
        assert kernel.objects.verify(digest, expected_size=size).size == size
    app = create_app(kernel, query_service=service)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "HTTP server failed to start"
            time.sleep(0.01)
        with httpx.Client(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=5,
                          trust_env=False) as client:
            health = client.get("/health")
            assert health.status_code == 200 and health.json()["status"] == "alive"
            item = state["queries"][3]
            response = client.post("/api/v1/queries/timeline", json=item["request"],
                headers={"Authorization": "Bearer " + state["tokens"]["admin"]})
            assert response.status_code == 200
            assert normalized(response.json()) == item["expected"]
            denied = client.post("/api/v1/queries/timeline", json=state["denials"][0]["request"],
                headers={"Authorization": "Bearer " + state["tokens"]["actor"]})
            assert denied.status_code == 403
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
    assert not thread.is_alive()
    with kernel.db.engine.connect() as conn:
        after = database_snapshot(conn)
    assert before == after, "Read/retry/startup changed historical data or duplicated effects"
    assert kernel.db.engine.pool.checkedout() == 0
    return {"status": "PASS", "schema": migration,
        "old_tables_byte_digest_preserved": len(state["snapshot"]) - 1,
        "added_empty_tables": sorted(added), "receipt_replays_identical": len(state["receipts"]),
        "historical_revisions_unchanged": len(revisions), "query_cases_equal": len(state["queries"]),
        "permission_denials_equal": len(state["denials"]), "task_records_equal": len(state["tasks"]),
        "objects_verified": len(state["objects"]), "http_real_tcp_statuses": [200, 200, 403],
        "database_unchanged_after_reads_and_retries": True, "connections_checked_out_at_end": 0,
        "database_snapshot": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seed", "verify"])
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    loaded = Path(inspect.getfile(Kernel)).resolve()
    assert loaded.is_relative_to(args.source_root.resolve() / "src"), loaded
    args.output.mkdir(parents=True, exist_ok=True)
    database = Database(pool_size=4)
    kernel = Kernel(database, ObjectStore(args.output / "objects"))
    try:
        result = (seed if args.operation == "seed" else verify)(kernel, args.output)
        result.update(label=args.label, loaded_kernel=str(loaded),
                      source_root=str(args.source_root.resolve()),
                      cpu_affinity=sorted(os.sched_getaffinity(0)))
        write(args.output / (args.label + ".json"), result)
        print(json.dumps({key: value for key, value in result.items() if key != "database_snapshot"}, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    main()
