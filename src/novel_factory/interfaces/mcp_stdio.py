"""NovelFactory local MCP server. Start with a private, server-provisioned binding file."""
import argparse
import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from novel_factory.application.audit_log import LogService
from novel_factory.application.agent_gateway import _inline_schema
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import CreativeControllerBinding, McpRuntime, ProjectBinding, TaskBinding
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def object_schema(*, optional=(), **properties):
    return {"type": "object", "properties": properties, "required": [key for key in properties if key not in optional],
            "additionalProperties": False}


_LOOKUP = object_schema(subject_id={"type": "string", "format": "uuid"},
                        predicate={"type": "string", "minLength": 1, "maxLength": 128})
_UUID = {"type": "string", "format": "uuid"}
_ARTIFACT = object_schema(artifact_id=_UUID, artifact_revision_id=_UUID,
                          sha256={"type": "string", "pattern": "^[a-f0-9]{64}$"})
_EVENT = object_schema(occurrence_id=_UUID, event_version_id=_UUID)
_REASON = {"type": "string", "minLength": 1, "maxLength": 10000}
_KEY = {"type": "string", "minLength": 1, "maxLength": 96}
_TOOLS = {
    "nf_harness_plan_propose": ("Derive bounded conditional alternatives from the assigned plan.",
        object_schema(optional=("horizon_scenes",), plan_ref=_ARTIFACT,
                      node_ids={"type": "array", "items": _UUID, "minItems": 1, "maxItems": 8, "uniqueItems": True},
                      trigger={"enum": ["ORDINARY", "MOTIVE_CONFLICT", "IRREVERSIBLE"]},
                      quota=_inline_schema("Quota", "harness"), horizon_scenes={"type": "integer", "minimum": 1, "maximum": 32},
                      idempotency_key=_KEY)),
    "nf_harness_simulate": ("Evaluate a frozen candidate using enrolled actors' fixed knowledge cuts.",
        object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1}, candidate_ref=_ARTIFACT,
                      actor_tasks={"type": "array", "minItems": 1, "maxItems": 16, "uniqueItems": True,
                                   "items": object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1})},
                      max_branches={"type": "integer", "minimum": 1, "maximum": 32},
                      max_steps_per_branch={"type": "integer", "minimum": 1, "maximum": 128},
                      seed={"type": "integer", "minimum": 0, "maximum": 2147483647}, idempotency_key=_KEY)),
    "nf_harness_plan_decide": ("Submit a plan decision to the existing adoption service, or decline every alternative.",
        object_schema(optional=("rolling_review_ref",), task_id=_UUID, generation={"type": "integer", "minimum": 1}, candidate_ref=_ARTIFACT,
                      option_id={"anyOf": [_UUID, {"type": "null"}]},
                      rolling_review_ref={"anyOf": [_ARTIFACT, {"type": "null"}]}, idempotency_key=_KEY)),
    "nf_change_point_request": ("Record evidence for a bounded revision to the volume plan.",
        object_schema(volume=_UUID, prior_plan_ref=_ARTIFACT,
                      event_refs={"type":"array","minItems":1,"maxItems":32,"items":_EVENT},
                      reason=_REASON, idempotency_key=_KEY)),
    "nf_rolling_window_submit": ("Submit a bounded future window for independent review.",
        object_schema(change_point_ref=_ARTIFACT, window={"type":"object"}, idempotency_key=_KEY)),
    "nf_change_point_review": ("Record an independent decision on the exact candidate and window.",
        object_schema(change_point_ref=_ARTIFACT, plan_ref=_ARTIFACT, window_ref=_ARTIFACT,
                      decision={"enum":["APPROVED","RETURNED"]}, reason=_REASON, idempotency_key=_KEY)),
    "nf_project_status": ("Read task counts for the configured project branch.", object_schema()),
    "nf_resume_project": ("Recover eligible work and wake satisfied waits in the configured branch.", object_schema()),
    "nf_task_get": ("Read the bound task's current execution identity.", object_schema()),
    "nf_context_get": ("Read the bound task's authorized context.", object_schema()),
    "nf_query_batch": ("Read selected state values at this task's bound time and knowledge cut.",
                       object_schema(lookups={"type": "array", "minItems": 1, "maxItems": 32, "items": _LOOKUP})),
    "nf_artifact_put": ("Store a task candidate with a stable idempotency key.",
                        object_schema(content={"type": "string", "minLength": 1, "maxLength": 32_768},
                                      idempotency_key={"type": "string", "minLength": 1, "maxLength": 96})),
}
_TOOLS["nf_change_point_request"][1]["properties"]["assessment"] = _inline_schema("DeviationAssessment", "agent")
_VALIDATORS = {name: Draft202012Validator(schema, format_checker=FormatChecker())
               for name, (_, schema) in _TOOLS.items()}


def read_private_config(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
        meta = os.fstat(source.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077 or meta.st_size > 16_384):
            raise ValueError("Invalid private configuration")
        return json.load(source)


def runtime_from_config(config):
    required = {"token", "database_url", "object_root", "binding"}
    if set(config) != required or not Path(config["object_root"]).is_absolute():
        raise ValueError("Invalid server configuration")
    binding = config["binding"]
    if set(binding) == {"work_id", "branch_id"}:
        bound = ProjectBinding(**binding)
    elif set(binding) == {"mode", "work_id", "branch_id"} and binding["mode"] == "creative_controller":
        bound = CreativeControllerBinding(binding["work_id"], binding["branch_id"])
    elif set(binding) == {"task_id", "generation", "worker_id"} and type(binding["generation"]) is int:
        bound = TaskBinding(**binding)
    else:
        raise ValueError("Invalid binding")
    kernel = Kernel(Database(config["database_url"], pool_size=2), ObjectStore(config["object_root"]))
    return McpRuntime(kernel, kernel.authenticate(config["token"]), bound)


def tool_error(code):
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=code)],
                                structuredContent={"error": {"code": code}})


def make_server(runtime, *, ceiling=None):
    server = Server("NovelFactory", version="2.1.0")

    @server.list_tools()
    async def list_tools():
        try:
            names = await asyncio.to_thread(runtime.available_tools)
        except DomainError:
            return []
        except Exception:
            return []
        return [types.Tool(name=name, description=_TOOLS[name][0], inputSchema=_TOOLS[name][1])
                for name in names if ceiling is None or name in ceiling]

    async def invoke(name, arguments):
        try:
            if (ceiling is not None and name not in ceiling) or name not in await asyncio.to_thread(runtime.available_tools):
                return tool_error("VIEW_DENIED")
            if not _VALIDATORS[name].is_valid(arguments):
                return tool_error("INVALID_ARGUMENT")
            if name in {"nf_project_status", "nf_resume_project"}:
                return await asyncio.to_thread(runtime.project, resume=name == "nf_resume_project")
            methods = {
                "nf_harness_plan_propose": "propose_creative_plan", "nf_harness_simulate": "simulate_creative_plan",
                "nf_harness_plan_decide": "decide_creative_plan", "nf_rolling_window_submit": "submit_window",
                "nf_change_point_request": "request_change", "nf_change_point_review": "review_change",
                "nf_task_get": "task_get", "nf_context_get": "context_get",
                "nf_query_batch": "query_batch", "nf_artifact_put": "artifact_put",
            }
            method = getattr(runtime, methods[name])
            return await asyncio.to_thread(method, **arguments)
        except DomainError as error:
            return tool_error(error.code)
        except Exception:
            # Drivers and providers may embed DSNs, paths or supplied data in exception messages.
            return tool_error("SERVICE_UNAVAILABLE")

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        started = time.monotonic()
        try:
            invocation, scope = await asyncio.to_thread(
                LogService.begin_tool, runtime, name if name in _TOOLS else "UNREGISTERED",
                server.request_context.request_id,
            )
        except Exception:
            return tool_error("SERVICE_UNAVAILABLE")
        result = await invoke(name, arguments)
        error = result.structuredContent["error"]["code"] if isinstance(result, types.CallToolResult) and result.isError else None
        evidence_id = None
        if isinstance(result,dict):
            # Only a recognized successful response can bind an audit object. Never
            # copy a rejected lookup, candidate content, or caller-supplied ID.
            for key in ("artifact_ref","change_point_ref","window_ref","review_ref","candidate_ref","report_ref","plan_ref"):
                ref = result.get(key)
                if isinstance(ref,dict) and ref.get("artifact_revision_id"):
                    evidence_id = ref["artifact_revision_id"]
                    break
            if evidence_id is None and name in {"nf_task_get","nf_context_get"}:
                evidence_id = result.get("context_bundle_id")
        try:
            await asyncio.to_thread(LogService.finish_tool, runtime, invocation, scope, error,
                                    round((time.monotonic()-started)*1000, 3), evidence_id)
        except Exception:
            # The attempt is already durable. A mutating tool has its own authoritative
            # idempotency receipt; a missing diagnostic must not turn success into a retry.
            print("NovelFactory MCP completion audit unavailable", file=sys.stderr)
        return result

    return server


async def character_input():
    """Bound untrusted Host traffic before the SDK decodes JSON or queues work."""
    for _ in range(128):
        line = await asyncio.to_thread(sys.stdin.buffer.readline, 1_048_577)
        if not line or len(line) > 1_048_576 or not line.endswith(b"\n"):
            return
        try:
            yield line.decode("utf-8")
        except UnicodeError:
            return


async def serve(config, *, character_only=False):
    runtime = runtime_from_config(config)
    try:
        if character_only:
            with runtime.kernel.db.uow(write=False) as repo:
                _, grant, _ = runtime._task(repo)
                if grant["role_id"] != "character_actor":
                    raise ValueError("Character binding required")
        server = make_server(runtime)
        async with stdio_server(stdin=character_input() if character_only else None) as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        runtime.kernel.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--character-only", action="store_true", help="Require a live character task binding")
    args = parser.parse_args()
    try:
        asyncio.run(serve(read_private_config(args.binding), character_only=args.character_only))
    except KeyboardInterrupt:
        return
    except Exception:
        print("NovelFactory MCP startup failed; check private configuration, grant and database.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
