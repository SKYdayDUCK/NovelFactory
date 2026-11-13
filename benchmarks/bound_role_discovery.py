"""Measure authorized tool discovery for a live frozen character role on an isolated PG17."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import tempfile
from time import perf_counter

from sqlalchemy import event

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application import dynamic_runtime, mcp_runtime
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore


def measure(output):
    db = Database(pool_size=2)
    try:
        with tempfile.TemporaryDirectory() as objects:
            kernel = Kernel(db, ObjectStore(objects))
            world = load_fixture(kernel)
            adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions",
                                              model="fixture", api_key=None)
            role = RuntimeTaskService(kernel, AgentGateway(kernel, {"fixture": adapter})).prepare(
                world["principal"], world["world_ref"], role_id="character_actor",
                task_kind="character_action", actor_id=world["ids"]["protagonist"],
                story_time=point(1), subject_ids=[world["ids"]["sword"]], predicates=["owner"],
                assignment="Propose an action", adapter_id="fixture")
            runtime = mcp_runtime.McpRuntime(kernel, role.principal, mcp_runtime.TaskBinding(
                role.task["task_id"], role.task["generation"], role.worker_id))
            expected = {"nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
            assert set(runtime.available_tools()) == expected
            sql_count = 0

            @event.listens_for(db.engine, "before_cursor_execute")
            def count_sql(*_):
                nonlocal sql_count
                sql_count += 1

            samples = []
            for _ in range(20):
                sql_count = 0
                start = perf_counter()
                assert set(runtime.available_tools()) == expected
                samples.append({"ms": round(1000 * (perf_counter() - start), 4), "sql": sql_count})
            result = {
                "python": platform.python_version(), "os": platform.system(), "db_pool": 2,
                "workload": "one active static role, warm discovery, sequential, zero provider calls",
                "source_sha256": {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                                  for m in (mcp_runtime, dynamic_runtime)},
                "samples": samples, "median_ms": statistics.median(row["ms"] for row in samples),
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    measure(parser.parse_args().output)
