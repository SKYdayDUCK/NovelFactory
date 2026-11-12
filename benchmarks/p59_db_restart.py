"""Crash and restart a freshly created local PG17 cluster while a DB coordinator stays alive."""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from experiments.db_execution.coordinator import Coordinator
from experiments.db_execution.operations import TaskStatus
from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import create_bound_task, load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import _error_response
from scripts.pitr_drill import _check, _config, _port, _run, _start, _stop


async def exercise(root, pg, result):
    data, config, log = root / "data", root / "postgres.conf", root / "postgres.log"
    port = _port()
    _run([str(pg / "initdb"), "-D", str(data), "-U", "nf_dev", "--encoding=UTF8", "--locale=C",
          "--auth-local=reject", "--auth-host=trust", "--data-checksums"])
    _config(config, {"listen_addresses": "127.0.0.1", "unix_socket_directories": "", "port": port,
                     "max_connections": 16, "shared_buffers": "32MB", "timezone": "UTC",
                     "fsync": "on", "synchronous_commit": "on", "full_page_writes": "on",
                     "logging_collector": "off", "log_min_messages": "log"})
    server = _start(pg, data, config, port, log)
    db = None
    try:
        url = f"postgresql+psycopg://nf_dev@127.0.0.1:{port}/nf_restart"
        _run([str(pg / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "nf_dev", "nf_restart"])
        _run([sys.executable, "-m", "alembic", "upgrade", "head"], env={**os.environ, "NF_DATABASE_URL": url})
        db = Database(url, pool_size=2)
        kernel = Kernel(db, ObjectStore(root / "objects"), joined_task_status=True)
        book = load_fixture(kernel)
        owner, ref = book["principal"], book["world_ref"]
        task = create_bound_task(kernel, owner, ref, point(0))
        op = TaskStatus(owner, task["task_id"])
        expected = kernel.get_task(owner, op.task_id)
        budget = kernel.tasks.create_budget(ref["work_id"], "5")
        spend = {"budget_id": budget, "amount": "0.50", "request_hash": "a" * 64,
                 "token_amount": 100, "call_key": "outage-fixture"}
        args = (op.task_id, task["generation"], task["worker_id"])
        attempt = kernel.tasks.reserve_call(*args, **spend)
        kernel.tasks.mark_call_sent(attempt["attempt_id"], worker_id=task["worker_id"])
        kernel.tasks.settle_call(attempt["attempt_id"], state="UNKNOWN")
        async with Coordinator(kernel, workers=2, capacity=4, batch=4, split_queues=True,
                               join_input=True) as coordinator:
            _check(await coordinator.submit(op) == expected, "Warm status mismatch")
            initial_pid = server.pid
            await asyncio.to_thread(_run, [str(pg / "pg_ctl"), "-D", str(data), "-w", "-m", "immediate", "stop"])
            await asyncio.to_thread(server.wait, timeout=15)
            outage = time.monotonic()
            probe = await asyncio.to_thread(subprocess.run,
                [str(pg / "pg_isready"), "-h", "127.0.0.1", "-p", str(port)],
                check=False, capture_output=True, timeout=3)
            _check(probe.returncode == 2, "Stopped PostgreSQL still answers")
            failures = await asyncio.gather(*(coordinator.submit(op) for _ in range(3)), return_exceptions=True)
            _check(all(isinstance(error, DomainError) and error.code == "SERVICE_UNAVAILABLE"
                       and error.retryable and error.recovery == "WAIT" for error in failures),
                   "Outage did not fail every item closed")
            responses = [_error_response(error, "outage-probe") for error in failures]
            _check(all(r.status_code == 503 and r.headers.get("Retry-After") for r in responses),
                   "Temporary database outage produced the wrong HTTP response mapping")
            server = await asyncio.to_thread(_start, pg, data, config, port, log)
            _check(server.pid != initial_pid, "Expected a new postmaster")
            recovered = await asyncio.gather(*(coordinator.submit(op) for _ in range(3)))
            elapsed = time.monotonic() - outage
            _check(all(row == expected for row in recovered), "Restarted coordinator returned different task data")
            result.update(stopped_pg_isready_exit=probe.returncode, distinct_postmaster=server.pid != initial_pid,
                          outage_rejections=[error.code for error in failures],
                          http_error_serializer_statuses=[response.status_code for response in responses],
                          recovered_verified_reads=len(recovered),
                          outage_to_verified_read_seconds=elapsed, coordinator_counts=dict(coordinator.counts))
        repeated = kernel.tasks.reserve_call(*args, **spend)
        _check(repeated["attempt_id"] == attempt["attempt_id"] and repeated["state"] == "UNKNOWN",
               "Idempotency identity or UNKNOWN state changed")
        try:
            kernel.tasks.mark_call_sent(attempt["attempt_id"], worker_id=task["worker_id"])
        except DomainError as error:
            _check(error.code == "INVALID_CALL_TRANSITION", "Unexpected provider resend error")
            result["resend_denied"] = error.code
        else:
            raise RuntimeError("UNKNOWN provider attempt was granted a second dispatch")
        with db.uow(write=False) as repo:
            row = repo.one("budgets", {"budget_id": budget})
            calls = repo.many("call_attempts", {"task_id": op.task_id})
        _check(len(calls) == 1 and row["reserved_amount"] == Decimal("0.5") and row["spent_amount"] == 0,
               "Duplicate provider attempt, reservation or billing")
        result.update(unique_attempts=len(calls), reserved=str(row["reserved_amount"]), spent=str(row["spent_amount"]),
                      external_provider_calls=0, status="PASS")
    finally:
        if db:
            db.close()
        _stop(server)
        result["recovery_log"] = [line for line in log.read_text().splitlines()
                                  if "recovery" in line or "redo " in line or "was interrupted" in line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, default=os.getenv("PG_BIN", "/usr/lib/postgresql/17/bin"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pg = args.pg_bin.resolve()
    for key in tuple(os.environ):
        if key.startswith("PG") and key != "PG_BIN":
            del os.environ[key]
    version = _run([str(pg / "postgres"), "--version"])["stdout"]
    _check("PostgreSQL) 17." in version, "PostgreSQL 17 required")
    result = {"status": "FAILED", "postgres": version, "shutdown_mode": "immediate",
              "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    try:
        with tempfile.TemporaryDirectory(prefix="nf-candidate-restart-") as root:
            asyncio.run(exercise(Path(root), pg, result))
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
