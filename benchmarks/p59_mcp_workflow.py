"""Exercise project-scoped MCP control over the durable P5.9 two-book workload."""
import argparse
from collections import Counter
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from time import perf_counter

from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import text

from benchmarks.p2_behavioral_load import isolated_kernel
from benchmarks.p59_workflow_load import trial
from novel_factory.infrastructure.private_files import create_private_file

ROOT = Path(__file__).resolve().parents[1]
PROJECT_TOOLS = {"nf_project_status", "nf_resume_project"}


@asynccontextmanager
async def open_session(path):
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(path)],
    )
    async with stdio_client(server) as streams, ClientSession(*streams) as session:
        yield session


class ProjectControl:
    def __init__(self, kernel, directory, stack):
        self.kernel = kernel
        self.directory = directory
        self.stack = stack
        self.portal = stack.enter_context(start_blocking_portal())
        self.channels = {}
        self.rows = []
        self.protocols = []
        self.handshakes = 0
        self.peak_sessions = 0
        self.wakes = Counter()
        self.started = perf_counter()

    def connect(self, index, book, reconnect=False):
        path = self.directory / f"project-{index}.json"
        if reconnect:
            _, channel = self.channels.pop(index)
            channel.close()
        else:
            scope = {name: book["world_ref"][name] for name in ("work_id", "branch_id")}
            grant = self.kernel.issue_grant(
                book["principal"], scope["work_id"], role_id="resume_operator",
                capabilities=["PROJECT_RESUME"], branch_ids=[scope["branch_id"]],
                expires_at=datetime.now(UTC) + timedelta(hours=4),
            )
            with os.fdopen(create_private_file(path), "w", encoding="utf-8") as out:
                json.dump({
                    "binding": scope, "token": grant["token"],
                    "database_url": self.kernel.db.engine.url.render_as_string(hide_password=False),
                    "object_root": str(self.kernel.objects.root),
                }, out)
        channel = self.stack.enter_context(ExitStack())
        session = channel.enter_context(self.portal.wrap_async_context_manager(open_session(path)))
        self.channels[index] = (session, channel)
        handshake = self.portal.call(session.initialize)
        self.handshakes += 1
        self.protocols.append(handshake.protocolVersion)
        if handshake.serverInfo.name != "NovelFactory":
            raise ValueError("Unexpected MCP peer")
        self.peak_sessions = max(self.peak_sessions, len(self.channels))
        sent = perf_counter()
        tools = sorted(tool.name for tool in self.portal.call(session.list_tools).tools)
        if set(tools) != PROJECT_TOOLS:
            raise ValueError("Project tool discovery differs from its grant")
        self.record(index, "reconnected" if reconnect else "connected", "tools/list", sent, tools)

    def record(self, index, stage, name, sent, result):
        self.rows.append({"book": index, "stage": stage, "operation": name,
                          "sent_s": sent - self.started, "latency_ms": (perf_counter() - sent) * 1000,
                          "verified_reply": result})

    def call(self, index, stage, name, book):
        sent = perf_counter()
        session, _ = self.channels[index]
        reply = self.portal.call(session.call_tool, name, {})
        if reply.isError or not isinstance(reply.structuredContent, dict):
            raise ValueError(f"MCP project call failed for {name}")
        elapsed_end = perf_counter()
        result = reply.structuredContent
        counts = {"READY": 0, "RUNNING": 0, "WAITING": 0}
        # These checkpoints have no concurrent worker, deadline or quota retries.
        # This oracle reads this project's durable states without using its MCP endpoint.
        with self.kernel.db.uow(write=False) as repo:
            for state, n in repo.conn.execute(text("""
                SELECT state,count(*) FROM nf_tasks WHERE work_id=:work AND branch_id=:branch
                GROUP BY state
            """), {"work": book["world_ref"]["work_id"], "branch": book["world_ref"]["branch_id"]}):
                counts["WAITING" if state == "BLOCKED" else state] = n
        if result["counts"] != counts or not result.get("resume_grant_expires_at"):
            raise ValueError("Project response differs from scoped PostgreSQL state")
        self.rows.append({"book": index, "stage": stage, "operation": name, "sent_s": sent - self.started,
                          "latency_ms": (elapsed_end - sent) * 1000,
                          "verified_reply": {"counts": result["counts"], "state": result["workflow_state"],
                                             "awakened": result.get("awakened")}})
        return result

    def stage(self, stage, index, book):
        if stage != "wake":
            self.connect(index, book, reconnect=stage == "drained")
        self.call(index, stage, "nf_project_status", book)
        awakened = 0
        while True:
            result = self.call(index, stage, "nf_resume_project", book)
            if not result["awakened"]:
                break
            if stage != "wake":
                raise ValueError("Unexpected wake before a prerequisite or after drain")
            awakened += result["awakened"]
        self.wakes[index] += awakened
        return awakened


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, required=True, choices=(10, 50, 200, 500, 1000, 2000))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    start = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        with isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], directory / "objects") as (kernel, _):
            with ExitStack() as stack:
                control = ProjectControl(kernel, directory, stack)
                result = trial(kernel, args.agents, project_stage=control.stage)
    book_outcomes = [Counter(row["state"] for row in result["records"] if row["book"] == index)
                     for index in range(2)]
    if (sum(control.wakes.values()) != result["awakened"] or control.peak_sessions != 2
            or control.handshakes != 4 or result["gate"] != "PASS"):
        result["gate"] = "FAILED"
    result.update(
        source_commit=commit, started_utc=start, python=platform.python_version(), platform=platform.platform(),
        driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        workflow_sha256=hashlib.sha256((ROOT / "benchmarks/p59_workflow_load.py").read_bytes()).hexdigest(),
        affinity=sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        transport_requests=len(control.rows), project_calls=sum(r["operation"] != "tools/list" for r in control.rows),
        mcp_session_handshakes=control.handshakes, mcp_session_peak=control.peak_sessions,
        stdio_server_process_peak=control.peak_sessions, protocols=control.protocols,
        wakes_via_mcp=dict(control.wakes), per_book_terminal_states=book_outcomes, mcp_requests=control.rows,
        measurement_boundary="MCP project discovery, status, durable wake and reconnect. Enrollment, "
                             "lease admission, fixture provider, result storage and world advance run in process.",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if args.output.suffix == ".gz" else open
    with opener(args.output, "wt", encoding="utf-8") as out:
        json.dump(result, out, ensure_ascii=False)
        out.write("\n")
    print(json.dumps({k: result[k] for k in (
        "population", "gate", "total_s", "drain_s", "states", "mcp_session_peak", "transport_requests",
        "wakes_via_mcp", "commercial_calls", "model_fixture_calls")}), flush=True)
    return int(result["gate"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
