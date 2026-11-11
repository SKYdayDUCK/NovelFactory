"""Measure real Workshop message commands and recovery in a fresh benchmark database."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
from time import perf_counter
from uuid import uuid4

from redis import Redis
from agent_workshop import server
from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.notifications import notify_batch, receive_batch, replay
from agent_workshop.organization import bootstrap


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latencies(rows, field):
    values = sorted(row[field] for row in rows if field in row)
    return {f"p{p}_ms": values[math.ceil(p / 100 * len(values)) - 1] if values else None
            for p in (50, 95, 99)}


def provision(dsn, size):
    org = uuid4().hex
    with connect(dsn) as conn:
        database = conn.execute("SELECT current_database() AS db").fetchone()["db"]
        if not database.startswith("aw_benchmark_") or conn.execute(
            "SELECT 1 FROM aw_organizations LIMIT 1"
        ).fetchone():
            raise RuntimeError("Provide a fresh, dedicated aw_benchmark_* database.")
        conn.execute("INSERT INTO aw_installations (installation_id,nf_instance_id) VALUES (%s,%s)",
                     (org, org))
        bootstrap(conn, org, installation_id=org)
        lead = {"database_url": dsn, "token": enroll(conn, org, org, "manager", 1, 1)}
        occupied = conn.execute("SELECT count(*) AS n FROM aw_employees WHERE org_id=%s",
                                (org,)).fetchone()["n"]
    rev = 1
    for n in range(occupied, size):
        receipt = server.dispatch(lead, "aw_hire", {
            "employee_id": f"worker-{n}", "operation_id": f"hire-{n}", "expected_revision": rev,
            "role_id": "character_actor", "supervisor_id": "volume", "team_id": "actors",
        })
        rev = receipt["revision"]
    with connect(dsn) as conn:
        rows = conn.execute("""SELECT DISTINCT ON (src) src,dst,kind,scope_ref FROM aw_edges
            WHERE org_id=%s AND graph_revision=%s ORDER BY src,dst,kind""", (org, rev)).fetchall()
        bindings = {e["employee_id"]: {"database_url": dsn, "token": enroll(
            conn, org, org, e["employee_id"], e["employment_revision"], 1)}
            for e in conn.execute("SELECT * FROM aw_employees WHERE org_id=%s", (org,))}
    if len(rows) != size or len(bindings) != size:
        raise ValueError("Active employee roster and routed senders must match the configured tier.")
    return org, rev, rows, bindings


def exchange(item, edge, revision, bindings):
    row = {"index": item, "sender": edge["src"], "recipient": edge["dst"], "kind": edge["kind"],
           "replay": item % 5 == 0, "completed": False}
    def call(name, args, actor, field):
        start = perf_counter()
        try:
            return server.dispatch(bindings[actor], name, args)
        finally:
            row[field] = (perf_counter() - start) * 1000
    try:
        send = {"operation_id": f"message-{item}", "recipient": edge["dst"], "kind": edge["kind"],
                "scope_ref": edge["scope_ref"], "graph_revision": revision, "ttl_seconds": 3600}
        msg = call("aw_message_send", send, edge["src"], "send_ms")
        row["message_id"] = msg["message_id"]
        ack = {"message_id": msg["message_id"]}
        receipt = call("aw_delivery_ack", ack, edge["dst"], "ack_ms")
        if receipt["status"] != "ACKNOWLEDGED":
            raise ValueError("Expected ACKNOWLEDGED")
        if row["replay"]:
            if msg != call("aw_message_send", send, edge["src"], "repeat_send_ms"):
                raise ValueError("Replay changed the message receipt")
            if receipt != call("aw_delivery_ack", ack, edge["dst"], "repeat_ack_ms"):
                raise ValueError("Replay changed the acknowledgment")
        row["completed"] = True
    except Exception as exc:
        row["error_type"] = type(exc).__name__
        # Domain codes are public; database and transport messages can contain connection data.
        if isinstance(exc, server.Denied):
            row["code"] = str(exc)
    return row


def recovery(dsn, redis, stream, expected, timeout):
    timer = perf_counter()
    while notify_batch(dsn, redis, stream):
        if perf_counter() - timer > timeout:
            return {"error": "initial_publish_timeout"}
    if redis.xlen(stream) != expected:
        return {"error": "initial_publication_count", "actual": redis.xlen(stream)}
    redis.delete(stream)  # only the random stream allocated to this isolated fixture
    recovered_at = perf_counter()
    reset_count = replay(dsn)
    while notify_batch(dsn, redis, stream):
        if perf_counter() - recovered_at > timeout:
            return {"error": "republish_timeout"}
    total = 0
    while total < expected and perf_counter() - recovered_at < timeout:
        delta = receive_batch(dsn, redis, stream, "capacity-inbox", "restarted")
        if not delta:
            break
        total += delta
    return {"replayed_outbox": reset_count, "rebuilt_stream": redis.xlen(stream),
            "distinct_consumed": total, "seconds": perf_counter() - recovered_at}


def run(size, contract):
    dsn = os.environ["AW_DATABASE_URL"]
    redis = Redis.from_url(os.environ["AW_REDIS_URL"], decode_responses=True, socket_timeout=5)
    org, rev, edges, bindings = provision(dsn, size)
    count = size * contract["rounds"]
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=contract["workers"]) as workers:
        records = list(workers.map(lambda i: exchange(i, edges[i % size], rev, bindings), range(count)))
    duration = perf_counter() - start
    finished = sum(r["completed"] for r in records)
    with connect(dsn) as conn:
        n = conn.execute("""SELECT count(*) AS messages, count(*) FILTER
            (WHERE d.acknowledged_at IS NOT NULL) AS acknowledged, count(o.event_id) AS pending_hints
            FROM aw_messages m JOIN aw_deliveries d USING(org_id,message_id)
            LEFT JOIN aw_outbox o ON o.org_id=m.org_id AND o.message_id=m.message_id AND o.last_notified IS NULL
            WHERE m.org_id=%s""", (org,)).fetchone()
        version = conn.execute("SHOW server_version").fetchone()["server_version"]
        migration = conn.execute("SELECT version_num FROM aw_alembic_version").fetchone()["version_num"]
    rec = recovery(dsn, redis, "workshop:capacity:" + org, n["pending_hints"], contract["timeout_seconds"])
    with connect(dsn) as conn:
        n["durable_inbox"] = conn.execute("""SELECT count(*) AS n FROM aw_inbox
            JOIN aw_outbox USING(event_id) WHERE org_id=%s AND subscriber=%s""",
            (org, "capacity-inbox")).fetchone()["n"]
    latency = {op: latencies(records, op + "_ms") for op in ("send", "ack")}
    gate = contract["gates"]
    all_receipts = finished == count == n["messages"] == n["acknowledged"] == n["pending_hints"] == n["durable_inbox"]
    isolated_counts = Counter(r["sender"] for r in records if r["completed"])
    passed = (all_receipts and len(isolated_counts) == size
              and all(n == gate["unique_messages_per_employee"] for n in isolated_counts.values())
              and rec.get("replayed_outbox") == rec.get("rebuilt_stream") == rec.get("distinct_consumed") == count
              and rec.get("seconds", math.inf) <= gate["max_notification_recovery_seconds"]
              and all(latency[op]["p99_ms"] is not None and
                      latency[op]["p99_ms"] <= gate["p99_" + op + "_ms"] for op in ("send", "ack")))
    source = Path(server.__file__).resolve().parents[3]
    return {"started_workshop_sha": subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip(),
            "implementation_hashes": {p.name: digest(p) for p in sorted(Path(server.__file__).parent.glob("*.py"))},
            "postgres": version, "workshop_migration": migration, "redis": redis.info("server")["redis_version"],
            "python": platform.python_version(), "workload": "SQL-backed MCP dispatcher; Redis held offline during exchange",
            "employees": size, "workers": contract["workers"], "rounds": contract["rounds"], "scheduled_messages": count,
            "unique_completed_exchanges": finished, "duration_seconds": duration, "completed_exchanges_per_second": finished / duration,
            "receipt_counts": n, "latency": latency, "notification_loss_and_restart": rec, "message_gate": "PASS" if passed else "NOT_MET",
            "nf_delegations": "NOT_MEASURED", "production_model_calls": 0, "supplier_cost": None,
            "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--employees", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = Path(__file__).with_name("capacity-contract.json")
    contract = json.loads(spec.read_text())
    if args.employees not in contract["tiers"]:
        parser.error("Tier outside the registered workload")
    result = run(args.employees, contract)
    result.update(contract_sha256=digest(spec), benchmark_sha256=digest(Path(__file__)), ended_at_utc=datetime.now(UTC).isoformat())
    args.output.parent.mkdir(exist_ok=True, parents=True)
    with args.output.open("x") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    return 0 if result["message_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
