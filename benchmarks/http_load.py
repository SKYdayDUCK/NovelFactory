"""Real HTTP logical-Agent load driver. No external model calls are made.

Input tokens are kept in memory and never included in the result. When invoked
as a CLI, use a private local fixture JSON and exclude it from review archives.
Latency includes unsuccessful responses; success-only distributions are separate.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import datetime as dt
import json
import math
from pathlib import Path
import random
import socket
import threading
import time

import httpx
from sqlalchemy import event
import uvicorn

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint


def percentiles(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    values = sorted(samples)
    def value(p):
        return round(values[max(0, math.ceil(p * len(values)) - 1)] * 1000, 4)
    return {"count": len(values), "p50_ms": value(.5), "p95_ms": value(.95), "p99_ms": value(.99)}


class SqlMeasurements:
    """Count actual SQL statements and peak checked-out connections."""
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.sql = self.transactions = self.checked_out = self.peak_checked_out = 0
        self.sql_seconds = self.max_sql_seconds = 0.0
        self.failed_statements = 0
        self.statement_kinds = Counter()
        self.transaction_seconds = self.max_transaction_seconds = 0.0
        self.finished_transactions = 0

    def _statement(self, conn, cursor, statement, parameters, context, executemany):
        with self.lock:
            self.sql += 1
            self.statement_kinds[statement.split()[0].upper()] += 1
        context.nf_measurement_started = time.perf_counter()

    def _record_statement_end(self, context, *, failed=False):
        started = getattr(context, 'nf_measurement_started', None)
        if started is None:
            return
        context.nf_measurement_started = None
        elapsed = time.perf_counter() - started
        with self.lock:
            self.sql_seconds += elapsed
            self.max_sql_seconds = max(self.max_sql_seconds, elapsed)
            self.failed_statements += int(failed)

    def _statement_done(self, conn, cursor, statement, parameters, context, executemany):
        self._record_statement_end(context)

    def _statement_error(self, exception_context):
        # Failed cursor execution skips after_cursor_execute. Count its wait
        # once as well; do not swallow or replace the database exception.
        self._record_statement_end(exception_context.execution_context, failed=True)

    def _begin(self, conn):
        conn.info['nf_transaction_started'] = time.perf_counter()
        with self.lock:
            self.transactions += 1

    def _transaction_done(self, conn):
        started = conn.info.pop('nf_transaction_started', None)
        if started is not None:
            elapsed = time.perf_counter()-started
            with self.lock:
                self.finished_transactions += 1
                self.transaction_seconds += elapsed
                self.max_transaction_seconds = max(self.max_transaction_seconds, elapsed)

    def _checkout(self, *args):
        with self.lock:
            self.checked_out += 1
            self.peak_checked_out = max(self.peak_checked_out, self.checked_out)

    def _checkin(self, *args):
        with self.lock:
            self.checked_out -= 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._statement)
        event.listen(self.engine, "after_cursor_execute", self._statement_done)
        event.listen(self.engine, "handle_error", self._statement_error)
        event.listen(self.engine, "begin", self._begin)
        event.listen(self.engine, "commit", self._transaction_done)
        event.listen(self.engine, "rollback", self._transaction_done)
        event.listen(self.engine, "checkout", self._checkout)
        event.listen(self.engine, "checkin", self._checkin)
        return self

    def __exit__(self, *args):
        event.remove(self.engine, "before_cursor_execute", self._statement)
        event.remove(self.engine, "after_cursor_execute", self._statement_done)
        event.remove(self.engine, "handle_error", self._statement_error)
        event.remove(self.engine, "begin", self._begin)
        event.remove(self.engine, "commit", self._transaction_done)
        event.remove(self.engine, "rollback", self._transaction_done)
        event.remove(self.engine, "checkout", self._checkout)
        event.remove(self.engine, "checkin", self._checkin)

    def result(self, duration: float) -> dict:
        return {"sql_statements": self.sql, "transactions": self.transactions,
                "transaction_seconds_before_commit": self.transaction_seconds,
                "max_transaction_seconds_before_commit": self.max_transaction_seconds,
                "finished_transactions": self.finished_transactions,
                "transaction_timing_note": "SQLAlchemy begin to before commit/rollback; end callbacks are not successful commit counts, and timing excludes final server commit wait",
                "sql_cursor_execution_seconds": self.sql_seconds,
                "failed_statements": self.failed_statements,
                "sql_timing_includes_failed_cursor_executions": True,
                "max_sql_cursor_execution_seconds": self.max_sql_seconds,
                "statement_kinds": dict(self.statement_kinds),
                "sql_timing_excludes_result_decoding": True,
                "sql_statements_per_second": round(self.sql / max(duration, 1e-9), 4),
                "peak_checked_out_connections": self.peak_checked_out,
                "checked_out_at_end": self.checked_out,
                "configured_pool_size": self.engine.pool.size(),
                "pool_status_at_end": self.engine.pool.status()}


@contextmanager
def loopback_server(app, *, lifespan="off", keepalive=5):
    """A real TCP Uvicorn server, with no ASGI in-process HTTP shortcut."""
    # asyncio enables TCP_NODELAY on accepted streams with an explicit TCP protocol.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False, lifespan=lifespan, timeout_keep_alive=keepalive))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("HTTP server failed to start")
            time.sleep(.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("HTTP server failed to stop")


async def run_load(base_url: str, clients: list[dict], *, rounds: int = 6,
                   mode: str = "closed_loop", arrival_rps: float = 100,
                   seed: int = 20261110, think_seconds: float = .01,
                   idle_observer=None) -> dict:
    """Each logical Agent has its own real task, grant and pinned query input."""
    if not clients or rounds < 1 or rounds > 100 or len(clients) > 200:
        raise ValueError("P1 fixture is bounded to 1..200 Agents and 1..100 rounds")
    if mode not in {"closed_loop", "open_loop"} or arrival_rps <= 0:
        raise ValueError("unsupported arrival model")
    status_counts, failures = Counter(), []
    latencies, success_latencies, schedule_delays = [], [], []
    successful_by_agent = Counter()
    started = time.monotonic()
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    emitted = completed = 0
    warmup_barrier = asyncio.Event()
    sleeping_agents = 0

    async with httpx.AsyncClient(base_url=base_url, timeout=15, trust_env=False,
            limits=httpx.Limits(max_connections=max(50, len(clients) * 2),
                               max_keepalive_connections=max(50, len(clients)))) as session:
        async def request_once(agent_index: int, sequence: int, scheduled_at=None):
            nonlocal emitted, completed
            client = clients[agent_index]
            begin = time.monotonic()
            if scheduled_at is not None:
                schedule_delays.append(max(0, begin - scheduled_at))
            emitted += 1
            try:
                response = await session.post(client.get("endpoint", "/api/v1/queries/timeline"),
                    headers={"Authorization": "Bearer " + client["token"]}, json=client["request"])
                elapsed = time.monotonic() - begin
                status_counts[str(response.status_code)] += 1
                body = response.json()
                if response.status_code == 200:
                    contract = client.get("response_contract", "UnifiedQueryResult")
                    validate_contract(contract, body, "2.1")
                    expected_ref = client["request"]["read_contract"]["revision"].get("world_ref")
                    if expected_ref is not None and body.get("read_cut", {}).get("world_ref") != expected_ref:
                        raise AssertionError("world_ref_mismatch")
                    if "expected_payload" in client and body.get("payload") != client["expected_payload"]:
                        raise AssertionError("fixture_payload_mismatch")
                    success_latencies.append(elapsed)
                    successful_by_agent[str(agent_index)] += 1
                else:
                    validate_contract("ErrorEnvelope", body)
                    failures.append({"agent_index": agent_index, "sequence": sequence,
                                     "http_status": response.status_code, "code": body["code"]})
            except Exception as exc:
                elapsed = time.monotonic() - begin
                status_counts["client_or_correctness_error"] += 1
                failures.append({"agent_index": agent_index, "sequence": sequence,
                                 "error_type": type(exc).__name__})
            finally:
                latencies.append(elapsed)
                completed += 1

        async def agent(index):
            nonlocal sleeping_agents
            rng = random.Random(seed + index)
            await request_once(index, 0)
            # All requests have returned. The following modeled external wait
            # must hold no database connection, regardless of Agent count.
            sleeping_agents += 1
            if sleeping_agents == len(clients):
                if idle_observer is not None:
                    idle_observer()
                warmup_barrier.set()
            await warmup_barrier.wait()
            for sequence in range(1, rounds):
                await asyncio.sleep(think_seconds * (.5 + rng.random()))
                await request_once(index, sequence)

        planned = len(clients) * rounds
        if mode == "closed_loop":
            await asyncio.gather(*(agent(i) for i in range(len(clients))))
        else:
            tasks = []
            arrival_start = time.monotonic()
            for number in range(planned):
                scheduled = arrival_start + number / arrival_rps
                await asyncio.sleep(max(0, scheduled - time.monotonic()))
                tasks.append(asyncio.create_task(request_once(number % len(clients), number, scheduled)))
            await asyncio.gather(*tasks)
    duration = time.monotonic() - started
    return {
        "evidence_kind": "real_loopback_http_with_real_postgresql",
        "started_at_utc": started_at, "duration_seconds": round(duration, 6),
        "logical_agents": len(clients), "rounds": rounds, "arrival_mode": mode,
        "arrival_rps_target": arrival_rps if mode == "open_loop" else None,
        "seed": seed, "modeled_think_seconds": think_seconds if mode == "closed_loop" else 0,
        "planned_requests": planned, "emitted_requests": emitted, "completed_requests": completed,
        "not_emitted": planned - emitted, "successful_requests": len(success_latencies),
        "status_counts": dict(status_counts), "failures": failures,
        "tool_requests_per_second": round(emitted / max(duration, 1e-9), 4),
        "valid_successes_per_second": round(len(success_latencies) / max(duration, 1e-9), 4),
        "latency_all_responses": percentiles(latencies),
        "latency_successes": percentiles(success_latencies),
        "arrival_schedule_delay": percentiles(schedule_delays),
        "successful_requests_by_agent": dict(successful_by_agent),
        "input_request_hashes": [fingerprint(client["request"]) for client in clients],
        "new_world_revisions": 0, "external_model_calls": 0,
        "scope": "P1 small fixture query load; not novel-generation or large-history capacity",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--mode", choices=["closed_loop", "open_loop"], default="closed_loop")
    parser.add_argument("--arrival-rps", type=float, default=100)
    args = parser.parse_args()
    clients = json.loads(args.fixture.read_text())["clients"]
    result = asyncio.run(run_load(args.base_url, clients, rounds=args.rounds,
                                 mode=args.mode, arrival_rps=args.arrival_rps))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items()
                      if key not in {"input_request_hashes", "successful_requests_by_agent", "failures"}}, indent=2))
    if result["status_counts"].get("client_or_correctness_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
