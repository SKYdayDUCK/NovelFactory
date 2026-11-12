"""Open-loop, one-host HTTP/1.1 load generator for the isolated database experiment.

The independent comparison reuses the frozen payload, validation and total
connection budget. Each process schedules disjoint global indices and owns its
event loop and persistent connections. The parent validates after the network
window. No retries, synthetic successes, hidden queue drops or stored tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import socket
import sys
import tempfile
import time
from collections import Counter
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext
from pathlib import Path

import h11

from benchmarks.http_load import percentiles
from novel_factory.contracts.registry import validate_contract


class Lane:
    """Exactly one request in flight on a persistent loopback connection."""

    def __init__(self):
        self.writer = None

    def close(self):
        if self.writer:
            self.writer.close()
            self.writer = None

    async def request(self, port, request, row, origin):
        if self.writer is None:
            self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
            self.protocol = h11.Connection(h11.CLIENT)
        method, path, token, body = request
        proto = self.protocol
        head = proto.send(
            h11.Request(
                method=method,
                target=path,
                headers=[
                    ("Host", f"127.0.0.1:{port}"),
                    ("Authorization", "Bearer " + token),
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                ],
            )
        )
        payload = proto.send(h11.Data(data=body)) + proto.send(h11.EndOfMessage())
        row["wire_start_seconds"] = time.monotonic() - origin
        self.writer.write(head + payload)
        await self.writer.drain()
        data = bytearray()
        while True:
            event = proto.next_event()
            if event is h11.NEED_DATA:
                proto.receive_data(await self.reader.read(65536))
            elif isinstance(event, h11.Response):
                row["response_status"] = event.status_code
                elapsed = dict(event.headers).get(b"x-nf-bench-server-us")
                if elapsed is not None:
                    row["server_to_headers_ms"] = int(elapsed) / 1000
            elif isinstance(event, h11.Data):
                data.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                if proto.our_state is h11.DONE and proto.their_state is h11.DONE:
                    proto.start_next_cycle()
                else:
                    self.close()
                return bytes(data)
            elif isinstance(event, h11.ConnectionClosed):
                raise EOFError("Incomplete HTTP response")


def usage():
    import resource

    current = resource.getrusage(resource.RUSAGE_SELF)
    return {"cpu_seconds": current.ru_utime + current.ru_stime, "peak_rss_kib": current.ru_maxrss}


def shard(channel, port, requests, count, target, worker, workers, slots, timeout):
    async def run():
        lanes = asyncio.LifoQueue()
        for _ in range(slots):
            lanes.put_nowait(Lane())
        before = usage()
        channel.send("ready")
        origin = channel.recv()

        async def one(index):
            planned = index / target
            dispatch = time.monotonic() - origin
            row = {
                "index": index,
                "scheduled_seconds": planned,
                "dispatch_seconds": dispatch,
                "valid": False,
                "status": "client_error",
            }
            lane = None
            try:
                async with asyncio.timeout(timeout):
                    lane = await lanes.get()
                    row["lane_wait_ms"] = (time.monotonic() - origin - dispatch) * 1000
                    row["attempt_seconds"] = time.monotonic() - origin
                    row["_content"] = await lane.request(
                        port, requests[index % len(requests)], row, origin
                    )
                    row["status"] = row["response_status"]
            except (OSError, EOFError, TimeoutError, h11.ProtocolError) as error:
                row.update(
                    status="timeout" if isinstance(error, TimeoutError) else "transport_error",
                    error_type=type(error).__name__,
                )
                if lane:
                    lane.close()
            finally:
                if lane:
                    lanes.put_nowait(lane)
            row["completed_seconds"] = time.monotonic() - origin
            return row

        tasks = []
        for index in range(worker, count, workers):
            await asyncio.sleep(max(0, origin + index / target - time.monotonic()))
            tasks.append(asyncio.create_task(one(index)))
        rows = await asyncio.gather(*tasks)
        while not lanes.empty():
            lanes.get_nowait().close()
        after = usage()
        after["cpu_seconds"] -= before["cpu_seconds"]
        return rows, after

    try:
        channel.send(asyncio.run(run()))
    finally:
        channel.close()


@contextmanager
def child_group():
    """Terminate only child processes started by this context after a harness failure."""
    context = mp.get_context("spawn")
    with ExitStack() as stack:

        def launch(fn, args):
            parent, child = context.Pipe()
            stack.callback(parent.close)
            proc = context.Process(target=fn, args=(child, *args))
            try:
                proc.start()
            finally:
                child.close()

            def finish():
                proc.join(3)
                if proc.is_alive():
                    proc.terminate()
                    proc.join()
                proc.close()

            stack.callback(finish)
            return parent

        yield launch


def receive(channel, timeout):
    if not channel.poll(timeout):
        raise TimeoutError("Benchmark child failed to report before deadline")
    return channel.recv()


def offer(port, requests, count, target, workers, connections, timeout):
    with child_group() as launch:
        channels = [
            launch(
                shard,
                (
                    port,
                    requests,
                    count,
                    target,
                    i,
                    workers,
                    connections // workers + (i < connections % workers),
                    timeout,
                ),
            )
            for i in range(workers)
        ]
        for ch in channels:
            if receive(ch, 90) != "ready":
                raise RuntimeError("Invalid worker handshake")
        start = time.monotonic() + 0.1
        for ch in channels:
            ch.send(start)
        groups = [receive(ch, count / target + timeout + 60) for ch in channels]
    return sorted((row for rows, _ in groups for row in rows), key=lambda row: row["index"]), [
        metrics for _, metrics in groups
    ]


class ServerTiming:
    def __init__(self, app, counts):
        self.app = app
        self.counts = counts

    async def __call__(self, scope, receive, send):
        self.counts["observed_received"] += 1
        started = time.monotonic()

        async def timed(event):
            if event["type"] == "http.response.start":
                elapsed = round((time.monotonic() - started) * 1e6)
                event["headers"] = list(event.get("headers", [])) + [
                    (b"x-nf-bench-server-us", str(elapsed).encode())
                ]
            await send(event)

        await self.app(scope, receive, timed)


def observed_worker(channel, listener, url, objects, processes, pool_size, layer):
    # This interpreter belongs exclusively to the benchmark. The production
    # app closure still performs the actual authentication, limiter and admission.
    from benchmarks import process_server

    if os.environ.get("NF_BENCH_PG_REVISION_WITNESS") == "1":
        from experiments.db_execution.revision_witness import WitnessKernel

        process_server.Kernel = WitnessKernel
    create_app = process_server.create_app

    def measured(*args, **kwargs):
        if layer == "auth":
            kwargs["query_service"].query = lambda principal, _: {
                "principal_id": str(principal.principal_id)
            }
        app = create_app(*args, **kwargs)
        counts = {"observed_received": 0, "observed_operation_entries": 0}
        controller = app.state.ingress if layer == "minimal" else app.state.admission
        slot = controller.slot
        ingress_snapshot = app.state.ingress.snapshot

        @asynccontextmanager
        async def observed_slot(*args, **kwargs):
            async with slot(*args, **kwargs):
                counts["observed_operation_entries"] += 1
                yield

        controller.slot = observed_slot
        app.state.ingress.snapshot = lambda: {**ingress_snapshot(), **counts}
        app.add_middleware(ServerTiming, counts=counts)
        return app

    process_server.create_app = measured
    process_server._worker(channel, listener, url, objects, processes, pool_size)


@contextmanager
def server(layer, url, objects, workers):
    from benchmarks import client_calibration as calibration
    from benchmarks import process_server

    with socket.socket() as listener, child_group() as launch:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        if layer == "transport":
            channels = [
                launch(calibration._server, (listener, calibration.response_body(2104), None))
            ]
        else:
            channels = [
                launch(observed_worker, (listener, url, objects, workers, 8 // workers, layer))
                for _ in range(workers)
            ]
        for ch in channels:
            if receive(ch, 40) != {"ready": True}:
                raise RuntimeError("Invalid server handshake")

        def snapshot(seconds):
            for ch in channels:
                ch.send("snapshot")
            snapshots = [receive(ch, 30) for ch in channels]
            return (
                snapshots[0]
                if layer == "transport"
                else process_server.aggregate(snapshots, seconds)
            )

        try:
            yield listener.getsockname()[1], snapshot
        finally:
            if layer != "transport":
                for ch in channels:
                    ch.send("stop")


def request_for(layer, actor):
    if layer == "minimal":
        return "GET", "/health", actor["token"], b""
    if layer == "auth-read":
        return "GET", "/api/v1/tasks/" + actor["task_id"], actor["token"], b""
    body = json.dumps(actor["state"], ensure_ascii=False, separators=(",", ":")).encode()
    return "POST", "/api/v1/queries/timeline", actor["token"], body


def validate(rows, fixture, layer):
    if layer == "query":
        from benchmarks.p3_open_arrival import validate_responses

        # The frozen predicate checks the immutable world reference and both
        # expected owners. Different subjects share that owner, so also bind
        # each successful response to the particular subjects/predicates sent.
        bodies = [row.get("_content") for row in rows]
        validate_responses(rows, fixture)
        for row, raw in zip(rows, bodies, strict=True):
            if not row["valid"]:
                continue
            request = fixture["population"][row["index"] % len(fixture["population"])]["state"]
            body = json.loads(raw)
            query = request["query"]
            expected = {(s, p) for s in query["subject_ids"] for p in query["predicates"]}
            observed = {
                (item["subject_id"], item["predicate"]) for item in body["payload"]["state_items"]
            }
            if (
                observed != expected
                or body["read_cut"]["knowledge_cut"] != request["knowledge_cut"]
            ):
                row.update(valid=False, correctness_error="cross_request_result")
        return
    population = fixture["population"]
    if layer == "transport":
        from benchmarks.client_calibration import response_body

        expected_transport = response_body(2104)
    for row in rows:
        data = row.pop("_content", None)
        if data is None:
            continue
        row["response_sha256"] = hashlib.sha256(data).hexdigest()
        actor = population[row["index"] % len(population)]
        try:
            if row["status"] != 200:
                error = json.loads(data)
                validate_contract("ErrorEnvelope", error)
                row["code"] = error["code"]
                if row["status"] not in (429, 503):
                    row["correctness_error"] = "unexpected_status"
                continue
            if layer == "transport":
                valid = data == expected_transport
            else:
                body = json.loads(data)
                if layer == "minimal":
                    valid = body == {"status": "alive", "release": "P0/P1"}
                elif layer == "auth":
                    valid = body == {"principal_id": str(actor["principal"].principal_id)}
                else:
                    valid = (
                        body["task_id"] == actor["task_id"]
                        and body["generation"] == actor["generation"]
                        and body["world_ref"] == fixture["world"]["world_ref"]
                    )
            row["valid"] = valid
            if not valid:
                row["correctness_error"] = "wrong_response"
        except (KeyError, TypeError, ValueError) as exc:
            row["correctness_error"] = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - retain all malformed error envelopes
            row["correctness_error"] = type(exc).__name__


def summarize(rows, count, target, metrics, layer):
    duration = max(row["completed_seconds"] for row in rows)
    indices = [r["index"] for r in rows]
    if indices != list(range(count)):
        raise RuntimeError("Lost, duplicated, or out-of-order sample")
    wire = [r["wire_start_seconds"] for r in rows if "wire_start_seconds" in r]
    wire_span = max(wire) - min(wire) if wire else 0
    wire_rate = (len(wire) - 1) / wire_span if wire_span else None
    status = Counter(str(r["status"]) for r in rows)
    validated = sum(r["valid"] for r in rows)
    if layer == "transport":
        received, accepted = metrics["requests_received"], metrics["responses_sent"]
    else:
        received = metrics["ingress"]["observed_received"]
        accepted = metrics["ingress"]["observed_operation_entries"]
    result = {
        "planned": count,
        "scheduled": len(rows),
        "attempted": sum("attempt_seconds" in r for r in rows),
        "headers_started": len(wire),
        "received_by_server": received,
        "admitted_by_server": accepted,
        "http_responses": sum(type(r["status"]) is int for r in rows),
        "validated": validated,
        "counts_by_status": dict(status),
        "errors_by_code": dict(Counter(r["code"] for r in rows if r.get("code"))),
        "timeouts": status["timeout"],
        "transport_errors": status["transport_error"],
        "http_429": status["429"],
        "http_409": status["409"],
        "http_5xx": sum(n for k, n in status.items() if k.isdecimal() and 500 <= int(k) < 600),
        "retry_attempts": 0,
        "business_validated": (validated if layer in ("auth-read", "query") else None),
        "network_seconds": duration,
        "validated_per_s": validated / duration,
        "requested_per_s": target,
        "header_start_per_s": wire_rate,
        "started_inside_offer_window": sum(t <= count / target for t in wire),
        "client_schedule_lag": percentiles(
            [r["dispatch_seconds"] - r["scheduled_seconds"] for r in rows]
        ),
        "client_lane_wait": percentiles(
            [r["lane_wait_ms"] / 1000 for r in rows if "lane_wait_ms" in r]
        ),
        "scheduled_to_completion": percentiles(
            [r["completed_seconds"] - r["scheduled_seconds"] for r in rows]
        ),
        "server_to_response_headers": percentiles(
            [r["server_to_headers_ms"] / 1000 for r in rows if "server_to_headers_ms" in r]
        ),
        "wire_rate_gate": "PASS"
        if len(wire) == count and (wire_rate or 0) >= 0.95 * target
        else "NOT_MET",
    }
    result["layer_gate"] = (
        "PASS"
        if result["wire_rate_gate"] == "PASS" and received == accepted == validated == count
        else "NOT_MET"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--layer", required=True, choices=("transport", "minimal", "auth", "auth-read", "query")
    )
    parser.add_argument("--agents", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--rate", type=float, default=1000)
    parser.add_argument("--workers", type=int, choices=(1, 2, 4, 8), default=4)
    parser.add_argument("--connections", type=int, default=256)
    parser.add_argument("--servers", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("The frozen receiver uses Linux resource and affinity instrumentation")
    from benchmarks import client_calibration as calibration
    from benchmarks.p2_behavioral_load import (
        DatabaseObserver,
        hardware,
        isolated_kernel,
        make_population,
    )

    if not __debug__:
        parser.error("Normal assertion mode is required by the frozen query validator")
    if not (
        1 <= args.agents <= 2000
        and 1 <= args.rounds <= 30
        and 0 < args.rate <= 5000
        and args.workers <= args.connections <= 512
        and 0 < args.timeout <= 120
    ):
        parser.error("Invalid bounded workload")
    if args.output.exists():
        parser.error("Choose a new output file")
    with ExitStack() as stack:
        source = os.getenv("NF_TEST_DATABASE_URL")
        objects = stack.enter_context(tempfile.TemporaryDirectory(prefix="nf-db-http-"))
        target = None
        if args.layer == "transport":
            fixture = calibration.synthetic_fixture(7)
            if args.agents > len(fixture["population"]):
                parser.error("Transport fixture has at most 1,000 identities")
            fixture["population"] = fixture["population"][: args.agents]
        else:
            if not source:
                parser.error("NF_TEST_DATABASE_URL is required")
            kernel, target = stack.enter_context(isolated_kernel(source, Path(objects)))
            fixture = make_population(kernel, args.agents, 0.8)
            kernel.db.engine.dispose()
        requests = [request_for(args.layer, actor) for actor in fixture["population"]]
        port, snapshot = stack.enter_context(server(args.layer, target, objects, args.servers))
        with DatabaseObserver(target) if target else nullcontext() as observer:
            rows, cpu = offer(
                port,
                requests,
                args.agents * args.rounds,
                args.rate,
                args.workers,
                args.connections,
                args.timeout,
            )
            metrics = snapshot(max(r["completed_seconds"] for r in rows))
        validation_start = time.monotonic()
        validation_cpu_start = time.process_time()
        validate(rows, fixture, args.layer)
        validation_duration = time.monotonic() - validation_start
        validation_cpu = time.process_time() - validation_cpu_start
        if target:
            with kernel.db.uow(write=False) as repo:
                if (
                    kernel.head(repo, fixture["world"]["world_ref"]["work_id"])
                    != fixture["world"]["world_ref"]
                ):
                    raise RuntimeError("Read benchmark changed the authoritative head")
        pg = observer.result() if observer else None
    summary = summarize(rows, len(requests) * args.rounds, args.rate, metrics, args.layer)
    topology = hardware()
    topology.update(
        load_client_location="separate spawned event loops with a shared parent validation process",
        api_processes=0 if args.layer == "transport" else args.servers,
        api_server_threads=0 if args.layer == "transport" else args.servers,
        transport_server_processes=int(args.layer == "transport"),
        generator_processes=args.workers,
    )
    topology["hardware_helper_sha256"] = topology.pop("benchmark_source_sha256")
    result = {
        "layer": args.layer,
        "schema": "p59-http-shards-2",
        "summary": summary,
        "raw_requests": rows,
        "server_metrics": metrics,
        "database_observer": pg,
        "client_workers": cpu,
        "client_validation_seconds": validation_duration,
        "client_validation_process_cpu_seconds": validation_cpu,
        "configuration": {
            **vars(args),
            "output": str(args.output),
            "pg_revision_witness": bool(target)
            and os.environ.get("NF_BENCH_PG_REVISION_WITNESS") == "1",
        },
        "environment": topology,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "world_unchanged": bool(target) if target else None,
        "effective_service_processes": 1 if args.layer == "transport" else args.servers,
        "notes": [
            "wire_start is the local write of HTTP headers; one attempt, no retries",
            "server_to_response_headers excludes socket write and is unavailable for the frozen transport control",
            "All clients and the validator run in separate processes on the same host",
            "Not the frozen AT-30; final validation CPU is outside the network window",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, separators=(",", ":"))
    print(json.dumps(summary, indent=2), flush=True)
    return int(summary["layer_gate"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
