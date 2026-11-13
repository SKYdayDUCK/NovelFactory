"""GENERATOR_CALIBRATION_ONLY: fixed-byte HTTP transport, never business capacity.

Reuse the unmodified p3_open_arrival.offer client/schedule against a separate
minimal Uvicorn process. The server never connects to PostgreSQL, authenticates
an Agent, validates a NovelFactory response, or commits world effects. Its
optimistic transport result cannot satisfy any application capacity gate.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import os
import resource
import socket
import threading
import time
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import uvicorn

from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import hardware
from benchmarks.p3_open_arrival import offer, rate_from_times

KIND = "GENERATOR_CALIBRATION_ONLY"


def synthetic_fixture(seed):
    namespace = uuid5(NAMESPACE_URL, KIND + ":" + str(seed))
    def identity(value):
        return str(uuid5(namespace, value))
    wr = {key: identity(key) for key in ("work_id", "branch_id", "world_revision_id")}
    population = []
    for index in range(1000):
        # Match the encoded STATE query's field layout and UUID widths. These
        # are transport bytes, not persisted tasks or application credentials.
        payload = {"schema_version": "2.1", "task_id": identity("task:" + str(index)), "generation": 1,
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr},
                "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": {"tick": 1, "phase": 0}},
            "query": {"kind": "STATE", "subject_ids": [identity("subject:" + str(0 if index < 800 else index))],
                "predicates": ["owner", "active_holder"]}, "page_size": 50, "cursor": None}
        population.append({"index": index, "state": payload,
            "token": "CALIBRATION_ONLY_" + "0" * 26})
    return {"population": population}


def response_body(size):
    prefix, suffix = b'{"kind":"GENERATOR_CALIBRATION_ONLY","padding":"', b'"}'
    if size < len(prefix) + len(suffix):
        raise ValueError("response size is too small for the calibration marker")
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


def _server(channel, listener, body, cpus):
    if cpus:
        os.sched_setaffinity(0, cpus)
    stats = {"requests_received": 0, "request_body_bytes": 0, "responses_sent": 0,
             "response_body_bytes": 0, "disconnected_requests": 0, "handler_errors": 0}

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        stats["requests_received"] += 1
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    stats["disconnected_requests"] += 1
                    return
                stats["request_body_bytes"] += len(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            stats["responses_sent"] += 1
            stats["response_body_bytes"] += len(body)
        except BaseException:
            stats["handler_errors"] += 1
            raise

    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                          lifespan="off", timeout_keep_alive=5))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Calibration server startup failed")
            time.sleep(0.01)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        baseline = (usage.ru_utime, usage.ru_stime)
        channel.send({"ready": True})
        if channel.recv() != "snapshot":
            raise RuntimeError("Unexpected calibration control command")
        usage = resource.getrusage(resource.RUSAGE_SELF)
        channel.send({**stats, "user_cpu_seconds": usage.ru_utime - baseline[0],
            "system_cpu_seconds": usage.ru_stime - baseline[1], "peak_rss_kib": usage.ru_maxrss,
            "affinity_cpus": sorted(os.sched_getaffinity(0))})
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        channel.close()


def run_once(fixture, size, cpus):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    body = response_body(size)
    process = context.Process(target=_server, args=(child, listener, body, cpus))
    process.start()
    child.close()
    try:
        if not parent.poll(20) or parent.recv() != {"ready": True}:
            raise RuntimeError("Calibration service did not become ready")
        before = resource.getrusage(resource.RUSAGE_SELF)
        rows, duration = asyncio.run(offer(f"http://127.0.0.1:{listener.getsockname()[1]}", fixture,
                                          target_rps=1000, rounds=5, client_connections=256))
        after = resource.getrusage(resource.RUSAGE_SELF)
        parent.send("snapshot")
        if not parent.poll(10):
            raise RuntimeError("Calibration service metrics timed out")
        server = parent.recv()
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        listener.close()
    expected_hash = hashlib.sha256(body).hexdigest()
    validation_started = time.thread_time()
    for row in rows:
        content = row.pop("_content", None)
        row.pop("valid", None)  # Not a NovelFactory contract-valid response.
        row["transport_bytes_match"] = content is not None and content == body
        if content is not None:
            row["response_sha256"] = hashlib.sha256(content).hexdigest()
    validation_cpu = time.thread_time() - validation_started
    wire = [row["wire_start_seconds"] for row in rows if "wire_start_seconds" in row]
    request_sizes = [len(json.dumps(item["state"], separators=(",", ":"), ensure_ascii=False).encode())
                     for item in fixture["population"]]
    received = sum(row.get("response_bytes", 0) for row in rows)
    return {"evidence_kind": KIND, "response_body_size_bytes": size,
        "response_body_sha256": expected_hash, "scheduled_requests": 5000, "recorded_attempts": len(rows),
        "target_header_start_rps": 1000, "client_connections_limit": 256,
        "observed_header_start_rps": rate_from_times(wire), "header_started_attempts": len(wire),
        "dispatch_start_rps": rate_from_times([row["dispatch_seconds"] for row in rows]),
        "all_http_status_counts": dict(Counter(str(row["status"]) for row in rows)),
        "transport_error_types": dict(Counter(row["error_type"] for row in rows if "error_type" in row)),
        "transport_bytes_matched": sum(row["transport_bytes_match"] for row in rows),
        "arrival_delay": percentiles([row["arrival_delay_ms"] / 1000 for row in rows]),
        "all_attempt_latency": percentiles([row["latency_ms"] / 1000 for row in rows]),
        "scheduled_to_completion": percentiles([row["completed_seconds"] - row["scheduled_seconds"] for row in rows]),
        "network_window_seconds": duration,
        "client": {"user_cpu_seconds": after.ru_utime - before.ru_utime,
            "system_cpu_seconds": after.ru_stime - before.ru_stime, "process_peak_rss_kib": after.ru_maxrss,
            "post_window_byte_validation_thread_cpu_seconds": validation_cpu,
            "affinity_cpus": sorted(os.sched_getaffinity(0)),
            "request_body_bytes_each_min": min(request_sizes), "request_body_bytes_each_max": max(request_sizes),
            "scheduled_request_body_bytes": sum(request_sizes) * 5, "received_response_body_bytes": received},
        "diagnostic_server": server, "novelfactory_capacity_verdict": "NOT_APPLICABLE",
        "novelfactory_business_commits": "NOT_APPLICABLE", "raw_attempts": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--response-bytes", type=int, nargs="+", default=[64, 2104])
    parser.add_argument("--repeats", type=int, choices=[2, 3], default=2)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--client-cpus", type=int, nargs="+")
    parser.add_argument("--server-cpus", type=int, nargs="+")
    parser.add_argument("--size-source", help="Path to actual load JSON used to select matched response size")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output file; previous observations are never overwritten")
    if any(not 64 <= size <= 100000 for size in args.response_bytes):
        parser.error("response bytes must be between 64 and 100000")
    allowed = os.sched_getaffinity(0)
    if any(not set(cpus) <= allowed for cpus in (args.client_cpus, args.server_cpus) if cpus):
        parser.error("requested CPU affinity exceeds this process's available CPUs")
    if args.client_cpus:
        os.sched_setaffinity(0, args.client_cpus)
    fixture = synthetic_fixture(args.seed)
    source = Path(args.size_source) if args.size_source else None
    environment = hardware()
    # hardware() also reports the original P2 harness topology. Replace that
    # topology; this transport probe contains no NovelFactory API processes.
    environment.update(load_client_location="separate process from spawned fixed-byte diagnostic server",
        api_processes=0, api_server_threads=0, diagnostic_server_processes=1,
        diagnostic_server_threads=1)
    environment["hardware_helper_source_sha256"] = environment.pop("benchmark_source_sha256")
    result = {"schema_version": "client-calibration-1", "evidence_kind": KIND,
        "novelfactory_capacity_verdict": "NOT_APPLICABLE", "novelfactory_business_commits": "NOT_APPLICABLE",
        "environment": environment, "seed": args.seed,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "reused_offer_source_sha256": hashlib.sha256(Path(offer.__code__.co_filename).read_bytes()).hexdigest(),
        "response_size_source": None if source is None else {
            "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
        "runs": [], "limitations": [
            "Synthetic fixed-byte ASGI server; no database, auth, JSON parsing or NovelFactory domain work",
            "All identities and authorization header bytes are synthetic and never written to results",
            "Reuses the unchanged 1000-client/5000-request/1000-rps offer; no retries",
            "Header-start is an HTTPcore event, not packet capture or proof of application acceptance",
            "Byte counters describe HTTP body bytes, excluding headers and TCP framing",
            "Small acknowledgement is an optimistic transport reference, not proof of application capacity",
            "Shared host and short window do not establish sustained capacity or a production SLO",
            "Client peak RSS is process-lifetime high-water, not a per-run delta"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Alternate sizes between repetitions to expose drift instead of reporting
    # only a best run; every row remains in the final evidence.
    for repetition in range(args.repeats):
        for size in args.response_bytes:
            run = run_once(fixture, size, args.server_cpus)
            run["repetition"] = repetition + 1
            result["runs"].append(run)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({key: run[key] for key in ("evidence_kind", "response_body_size_bytes", "repetition",
                "recorded_attempts", "observed_header_start_rps", "all_http_status_counts",
                "transport_bytes_matched", "network_window_seconds")}), flush=True)
    return 0  # No application capacity gate exists in this calibration.


if __name__ == "__main__":
    raise SystemExit(main())
