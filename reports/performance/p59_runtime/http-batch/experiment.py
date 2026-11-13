"""Measure HTTP batch query validation and verified replies on a migrated disposable PG17."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
from time import perf_counter, process_time

import httpx

from benchmarks.http_load import loopback_server
from novel_factory.application import queries
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces import http as transport
from tests.service.conftest import make_client


def measure(output, *, paired=False):
    db = Database(pool_size=8)
    samples = []
    validated = []
    real_validate = queries.validate_contract

    def counted(name, value, version="2.0"):
        if name != "QueryBatchRequest":
            return real_validate(name, value, version)
        started = perf_counter()
        try:
            return real_validate(name, value, version)
        finally:
            validated.append((perf_counter() - started) * 1000)

    queries.validate_contract = transport.validate_contract = counted
    try:
        with tempfile.TemporaryDirectory() as directory:
            kernel = Kernel(db, ObjectStore(directory))
            world = load_fixture(kernel, "r1_sword")
            actor = make_client({"kernel": kernel}, world, actor=True)
            single = actor["request"]
            headers = {"Authorization": "Bearer " + actor["token"]}
            # The injected subclass receives an owned, validated dictionary and the
            # service validates it again, exactly as the frozen stock HTTP batch route.
            class DictionaryService(queries.WorldQueryService):
                pass

            apps = {"prepared": transport.create_app(kernel)}
            if paired:
                apps["dictionary"] = transport.create_app(kernel, query_service=DictionaryService(kernel))
            with ExitStack() as stack:
                clients = {}
                for name, app in apps.items():
                    url = stack.enter_context(loopback_server(app))
                    clients[name] = stack.enter_context(httpx.Client(base_url=url, trust_env=False))
                for count in (1, 16):
                    body = {key: single[key] for key in ("schema_version", "task_id", "generation",
                                                         "read_contract", "knowledge_cut")}
                    item = {key: single[key] for key in ("subject_time", "query", "page_size")}
                    body["items"] = [{"item_id": str(i), **deepcopy(item)} for i in range(count)]
                    for run in range(31 if paired else 15):
                        order = list(clients) if run % 2 else list(reversed(clients))
                        for name in order:
                            validated.clear()
                            start, cpu_start = perf_counter(), process_time()
                            reply = clients[name].post("/api/v1/queries/batch", json=body, headers=headers)
                            elapsed, cpu = (perf_counter() - start) * 1000, (process_time() - cpu_start) * 1000
                            reply.raise_for_status()
                            data = reply.json()
                            assert data["all_items_succeeded"] and len(data["items"]) == count
                            assert all(row["payload"] == actor["expected_payload"] for row in data["items"])
                            if run:
                                samples.append({"items": count, "pair": run, "arm": name,
                                                "latency_ms": round(elapsed, 4), "cpu_ms": round(cpu, 4),
                                                "validation_ms": round(sum(validated), 4),
                                                "validations": len(validated)})
        output.parent.mkdir(parents=True, exist_ok=True)
        result = {"workload": "sequential loopback HTTP, one live BELIEF actor, PG17, no model",
                  "source_sha256": {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                                    for m in (queries, transport)}, "samples": samples}
        for name in apps:
            result[name] = {}
            for count in (1, 16):
                rows = [row for row in samples if row["items"] == count and row["arm"] == name]
                result[name][str(count)] = {key: round(statistics.median(row[key] for row in rows), 4)
                                            for key in ("latency_ms", "cpu_ms", "validation_ms", "validations")}
        output.write_text(json.dumps(result, indent=2) + "\n")
    finally:
        queries.validate_contract = transport.validate_contract = real_validate
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--paired", action="store_true")
    args = parser.parse_args()
    measure(args.output, paired=args.paired)
