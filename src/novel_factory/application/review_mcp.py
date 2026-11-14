"""Native review tools backed by real, task-bound MCP requests and immutable receipts.

The model receives tool schemas and results only. The broker retains the role
Principal, live lease and database; no capability token enters the Host process.
"""
import json
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid5

from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import text

from ..domain.canonical import canonical_bytes, fingerprint
from .authorization import Principal
from .kernel import require
from .mcp_runtime import McpRuntime, TaskBinding
from .quality_memory import _record

REQUIRED_TOOLS = frozenset({"nf_context_get", "nf_query_batch"})
TOOLS = REQUIRED_TOOLS | {"nf_review_material"}
BLIND_TOOLS = frozenset({"nf_context_get", "nf_review_material"})
RECEIPT_KIND = "NATIVE_REVIEW_TOOL_RECEIPT"
PROVENANCE_KIND = "NATIVE_REVIEW_FINDING_PROVENANCE"
MAX_CALLS = 16
MAX_RESULT_BYTES = 1_000_000


def _provenance_source(kernel, repo, ref, work, kind):
    payload = kernel.artifact(repo, ref, work, kind=kind)["payload"]
    require(fingerprint(payload) == ref["sha256"], "INVALID_EVIDENCE", "Historical source bytes do not match their fixed hash")
    return payload


def _provenance_metadata(kernel, repo, task, scope):
    """Freeze version associations, never a judgment about the current prose.

    Only this writer's already recorded fact-reading history is eligible. Exact
    matching summaries can have several sources; return each verifiable version
    and leave the substantive judgment to the independent reader.
    """
    from .internal_review_resolution import _assignment_case_hash, _current
    from .production_pipeline import _load
    from .world_coverage_recovery import validate_reading_world

    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    identity = job and job["config"].get("production_node")
    if (task["role_id"], task["task_kind"]) != ("reviewer", "verification") or not identity:
        return None
    board = _load(kernel, repo, task["work_id"], task["branch_id"])
    node = board and board["nodes"].get(identity)
    require(node and node["stage"] == "internal_verification" and node["state"] == "ENROLLED"
            and node.get("task_id") == task["task_id"], "VIEW_DENIED", "Verification provenance has no active case")
    request = node["internal_case"]
    if request["source_kind"] != "DIAGNOSIS":
        return None
    writer = _current(board, request)
    frozen = kernel.artifact(repo, scope["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    require(board["world_ref"] == request["board_world_ref"]
            and validate_reading_world(kernel, repo, board, node) == request["world_ref"] == scope["world_ref"]
            and _assignment_case_hash(frozen["assignment"]) == request["case_hash"]
            and all(request[key] in frozen["material_refs"] for key in ("source_draft_ref", "report_ref")),
            "VIEW_DENIED", "Provenance must belong to the frozen diagnosis and materials")
    diagnosis = _provenance_source(kernel, repo, request["source_ref"], task["work_id"], "PRODUCTION_REASSESSMENT")
    original = diagnosis["request"]
    require(original["source_draft_ref"] == request["source_draft_ref"]
            and original["report_ref"] == request["report_ref"] and original["target"] == request["target"]
            and fingerprint(original) == request["diagnosis_request_sha256"],
            "INVALID_EVIDENCE", "Diagnosis provenance source changed")
    work, branch = task["work_id"], task["branch_id"]
    current = _provenance_source(kernel, repo, request["source_draft_ref"], work, "NARRATIVE_DRAFT")
    scene = _provenance_source(kernel, repo, current["scene_ref"], work, "SCENE_PLAN")
    findings = original["findings"]
    hashes = [{"finding_sha256": fingerprint(item), "summary_sha256": fingerprint(item["passage"])
               if isinstance(item.get("passage"), str) else None} for item in findings]
    allowed_drafts = {fingerprint(entry["output_ref"]): entry for entry in [writer, *writer.get("history", [])]
                      if entry.get("output_ref")}
    fact_node = board["nodes"].get("facts:" + request["target"].removeprefix("draft:"))
    require(fact_node and fact_node["stage"] == "facts" and fact_node["parents"] == [request["target"]],
            "INVALID_EVIDENCE", "Fact history is outside this writer's scene")
    candidates, seen = [], set()
    for entry in [fact_node, *fact_node.get("history", [])]:
        ref = entry.get("output_ref")
        if not ref or fingerprint(ref) in seen:
            continue
        seen.add(fingerprint(ref))
        wrapper = _provenance_source(kernel, repo, ref, work, "ROLLING_FACT_REPORT")
        extraction = _provenance_source(kernel, repo, wrapper["extraction_ref"], work, "ASSERTION_EXTRACTION")
        report = _provenance_source(kernel, repo, wrapper["reconciliation_ref"], work, "RECONCILIATION_REPORT")
        source = _provenance_source(kernel, repo, extraction["draft_ref"], work, "NARRATIVE_DRAFT")
        source_writer = allowed_drafts.get(fingerprint(extraction["draft_ref"]))
        reader = repo.one("tasks", {"task_id": extraction["task_id"], "work_id": work, "branch_id": branch})
        origin = repo.one("tasks", {"task_id": source["task_id"], "work_id": work, "branch_id": branch})
        reader_input = repo.one("task_inputs", {"task_id": extraction["task_id"], "generation": extraction["generation"]})
        writer_input = repo.one("task_inputs", {"task_id": source["task_id"], "generation": source["generation"]})
        old_scene = _provenance_source(kernel, repo, source["scene_ref"], work, "SCENE_PLAN")
        require(source_writer and source_writer.get("task_id") == source["task_id"]
                and source_writer.get("generation", source["generation"]) == source["generation"]
                and reader and reader["role_id"] == "evidence_maintainer"
                and origin and origin["role_id"] == "writer" and writer_input
                and writer_input["spec"]["world_ref"] == source["input_world_ref"]
                and reader_input and reader_input["spec"]["world_ref"]["work_id"] == work
                and reader_input["spec"]["world_ref"]["branch_id"] == branch
                and entry.get("task_id") == extraction["task_id"]
                and entry.get("generation", extraction["generation"]) == extraction["generation"]
                and reader["generation"] >= extraction["generation"]
                and report["extraction_ref"] == wrapper["extraction_ref"]
                and report["draft_ref"] == extraction["draft_ref"]
                and report["text_ref"] == extraction["text_ref"] == source["text_ref"]
                and source["input_world_ref"]["work_id"] == work and source["input_world_ref"]["branch_id"] == branch
                and report["adopted_world_ref"]["work_id"] == work and report["adopted_world_ref"]["branch_id"] == branch
                and old_scene["scene_contract"]["scene_id"] == scene["scene_contract"]["scene_id"],
                "INVALID_EVIDENCE", "Historical fact report has a different writer, reader or text version")
        summary_hashes = {fingerprint(assertion["summary"]) for assertion in extraction["assertions"]}
        matched = [index for index, item in enumerate(hashes) if item["summary_sha256"] in summary_hashes]
        if matched:
            candidates.append({"report_ref": ref, "extraction_ref": wrapper["extraction_ref"],
                "reconciliation_ref": wrapper["reconciliation_ref"], "source_draft_ref": extraction["draft_ref"],
                "text_ref": source["text_ref"], "matched_finding_indices": matched,
                "matches_current": extraction["draft_ref"] == request["source_draft_ref"],
                "report_binding": {key: report[key] for key in ("draft_ref", "text_ref", "extraction_ref", "status")}})
    return {"version": 1, "case_hash": request["case_hash"], "report_ref": request["report_ref"], "current_draft_ref": request["source_draft_ref"],
            "current_text_ref": current["text_ref"], "source_scene_ref": current["scene_ref"],
            "scene_story_interval": scene["scene_contract"]["story_interval"], "diagnosis_ref": request["source_ref"],
            "finding_hashes": hashes, "candidates": candidates}


class _ReviewRuntime(McpRuntime):
    """Add only frozen version metadata to a legitimately read case report."""
    provenance = None

    def review_material(self, artifact_ref):
        value = super().review_material(artifact_ref)
        if not self.provenance or artifact_ref != self.provenance["report_ref"]:
            return value
        with self.kernel.db.uow(write=False) as repo:
            task, _, _, _ = self._bound_state(repo)
            record = self.kernel.artifact(repo, self.provenance["ref"], task["work_id"], kind=PROVENANCE_KIND)["payload"]
            require(all(record[key] == task[key] for key in ("task_id", "generation", "context_bundle_id")),
                    "VIEW_DENIED", "Historical version metadata belongs to another review")
        return {**value, "finding_provenance": record["provenance"], "finding_provenance_ref": self.provenance["ref"]}


def _completed_registration_query(value, arguments):
    """A completed verification lookup can report a typed missing registration.

    Denial, unavailable infrastructure and successfully returned UNKNOWN/PENDING
    values are distinct. They cannot be recast as missing registrations here.
    """
    items, lookups = value.get("items"), arguments.get("lookups")
    if value.get("all_items_succeeded") is not False or not isinstance(items, list) or not lookups:
        return False
    if len(items) != len(lookups) or {item.get("item_id") for item in items} != {str(i) for i in range(len(lookups))}:
        return False
    return (any(item.get("status") == "ERROR" for item in items)
            and all(item.get("status") == "SUCCESS" or item.get("status") == "ERROR"
                    and item.get("error", {}).get("code") in {"ENTITY_NOT_FOUND", "UNKNOWN_PREDICATE"} for item in items))


def _audit_rows(repo, task_id, generation, name, request_hash):
    return [dict(row) for row in repo.conn.execute(text("""
        SELECT * FROM nf_audit_events WHERE task_id=:task AND generation=:generation
          AND tool_name=:tool AND request_id=:request AND event_type='mcp.started'
    """), {"task": task_id, "generation": generation, "tool": name, "request": request_hash}).mappings()]


def _audit_pair(repo, receipt):
    started = repo.conn.execute(text("SELECT * FROM nf_audit_events WHERE event_id=:id"),
                               {"id": receipt["invocation_id"]}).mappings().one_or_none()
    finished = repo.conn.execute(text("""SELECT * FROM nf_audit_events
        WHERE causation_id=:id AND event_type='mcp.finished'"""),
        {"id": receipt["invocation_id"]}).mappings().all()
    require(started and len(finished) == 1, "REVIEW_QUERY_REQUIRED", "MCP invocation evidence is incomplete")
    finish = finished[0]
    for row in (started, finish):
        require(all(str(row[key]) == str(receipt[key]) for key in
                    ("work_id", "branch_id", "task_id", "generation", "role_id", "principal_id"))
                and str(row["world_revision_id"]) == receipt["world_ref"]["world_revision_id"]
                and row["request_id"] == receipt["mcp_request_sha256"] and row["tool_name"] == receipt["tool"],
                "REVIEW_QUERY_REQUIRED", "MCP invocation belongs to another frozen scope")
    require(started["event_type"] == "mcp.started" and finish["outcome"] == receipt["outcome"]
            and (receipt["outcome"] != "OK" or finish["error_code"] is None)
            and str(finish["parent_span_id"]) == receipt["invocation_id"]
            and started["event_time"] <= finish["event_time"] <= datetime.fromisoformat(receipt["lease_expiry"]),
            "REVIEW_QUERY_REQUIRED", "MCP invocation did not finish within its authorized lease")
    return started, finish


class ReviewMcpBroker:
    @classmethod
    def from_attempt(cls, kernel, request):
        """Provision from durable call identity, never from the model's message text."""
        with kernel.db.uow(write=False) as repo:
            call = repo.one("call_attempts", {"attempt_id": request.attempt_id})
            require(not request.dry_run and call and call["state"] == "SENT" and call["request_hash"] == request.request_sha256,
                    "VIEW_DENIED", "A sent, frozen native attempt is required")
            task = repo.one("tasks", {"task_id": call["task_id"], "generation": call["generation"]})
            require(task and task["role_id"] in {"reviewer", "chief_editor", "evidence_maintainer", "literary_editor"},
                    "VIEW_DENIED", "This task does not have a production reading role")
            prompt_row = repo.one("artifacts", {"work_id": task["work_id"], "kind": "AGENT_PROMPT",
                "artifact_revision_id": str(uuid5(UUID(request.attempt_id), "prompt:revision"))})
            require(prompt_row, "VIEW_DENIED", "Frozen native prompt is unavailable")
            prompt = kernel.artifact(repo, {k: prompt_row[k] for k in
                ("artifact_id", "artifact_revision_id", "sha256")}, task["work_id"], kind="AGENT_PROMPT")["payload"]
            require(fingerprint(prompt) == request.request_sha256 and prompt["messages"] == list(request.messages)
                    and prompt["task_id"] == task["task_id"] and prompt["generation"] == task["generation"]
                    and prompt["model_configuration"].get("adapter_kind") == "codex-cli"
                    and prompt["model_configuration"].get("auth_mode") == "host-login",
                    "VIEW_DENIED", "Native tools do not match the frozen prompt")
            principal = Principal(task["grant_id"], task["principal_id"])
            runtime = _ReviewRuntime(kernel, principal, TaskBinding(task["task_id"], task["generation"], task["lease_owner"]))
            task, grant, context, spec = runtime._bound_state(repo)
            require(isinstance(task["lease_owner"], str) and bool(task["lease_owner"]),
                    "VIEW_DENIED", "A native read broker requires an owned execution lease")
            scope = {key: task[key] for key in ("task_id", "generation", "work_id", "branch_id", "role_id", "principal_id", "grant_id", "context_bundle_id")}
            scope.update(attempt_id=request.attempt_id, request_sha256=request.request_sha256,
                         input_ref=prompt["input_ref"], world_ref=spec["world_ref"],
                         worker_id=task["lease_owner"], grant_epoch=grant["epoch"])
            frozen_input = kernel.artifact(repo, prompt["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
            scope["material_refs"] = frozen_input["material_refs"]
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
            scope["review_mcp_blind"] = bool(job and job["config"].get("review_mcp_blind"))
            scope["review_mcp_material_only"] = scope["review_mcp_blind"] or task["role_id"] == "literary_editor"
            if scope["review_mcp_blind"]:
                packet = job["config"].get("book_review_packet")
                require(task["role_id"] == "reviewer" and packet and frozen_input["material_refs"] == [packet]
                        and kernel.artifact(repo, packet, task["work_id"])["kind"] == "BOOK_READER_PACKET",
                        "VIEW_DENIED", "Blind MCP tools require the exact enrolled book-reader packet")
            compiled = kernel.artifact(repo, context["artifact_ref"], task["work_id"], kind="COMPILED_CONTEXT")["payload"]
            manifest = kernel.artifact(repo, compiled["input_manifest_ref"], task["work_id"], kind="INPUT_MANIFEST")["payload"]
            scope["query_scope"] = {"subject_ids": manifest["subject_ids"], "predicates": manifest["predicates"],
                                    "plane": context["read_cut"]["knowledge_cut"]["plane"]}
            headroom = call["reserved_tokens"] - prompt["model_configuration"]["max_output_tokens"] - len(canonical_bytes(prompt["messages"]))
            scope["result_byte_limit"] = min(MAX_RESULT_BYTES, spec["budget"]["token_budget"], headroom)
            require(scope["result_byte_limit"] > 0, "CAPACITY_LIMIT", "No reserved native input allowance remains for MCP results")
            require(len({fingerprint(ref) for ref in scope["material_refs"]}) + (1 if scope["review_mcp_material_only"] else 2) <= MAX_CALLS,
                    "CAPACITY_LIMIT", "Required review materials exceed the native tool-call allowance")
        # Retain the authenticated worker binding after acceptance clears the
        # mutable lease fields. This is controller-owned evidence, not model input.
        with kernel.objects.lock(), kernel.db.uow() as repo:
            repo.one("works", {"work_id": scope["work_id"]}, lock="share")
            current_task, _, _, _ = runtime._bound_state(repo)
            provenance = _provenance_metadata(kernel, repo, current_task, scope)
            if provenance:
                report_ref = provenance["report_ref"]
                record = {**{key: scope[key] for key in ("task_id", "generation", "context_bundle_id", "attempt_id",
                           "request_sha256", "input_ref", "world_ref")}, "report_ref": report_ref, "provenance": provenance}
                provenance_ref = _record(kernel, repo, scope["work_id"], principal.principal_id, request.attempt_id,
                                         "review-finding-provenance", record, PROVENANCE_KIND)
                scope.update(provenance_ref=provenance_ref, provenance_report_ref=report_ref)
                runtime.provenance = {"ref": provenance_ref, "report_ref": report_ref}
            binding_ref = _record(kernel, repo, scope["work_id"], principal.principal_id, request.attempt_id,
                                  "review-mcp-binding", scope, "NATIVE_REVIEW_TOOL_BINDING")
        return cls(kernel, runtime, {**scope, "binding_ref": binding_ref})

    def __init__(self, kernel, runtime, scope):
        self.kernel, self.runtime, self.scope = kernel, runtime, scope
        self.receipt_refs, self.tool_specs = [], []
        self._calls, self._total_bytes = {}, 0
        self.thread_id = self.turn_id = None
        self.allowed_tools = BLIND_TOOLS if scope["review_mcp_material_only"] else TOOLS
        self.required_tools = BLIND_TOOLS if scope["review_mcp_material_only"] else REQUIRED_TOOLS

    async def __aenter__(self):
        from ..interfaces.mcp_stdio import make_server
        server = make_server(self.runtime, ceiling=self.allowed_tools)
        self._connection = create_connected_server_and_client_session(server, read_timeout_seconds=timedelta(seconds=30))
        self._client = await self._connection.__aenter__()
        try:
            tools = (await self._client.list_tools()).tools
            names = {tool.name for tool in tools}
            require(self.required_tools <= names <= self.allowed_tools and (not self.scope["material_refs"] or "nf_review_material" in names),
                    "VIEW_DENIED", "The required read-only MCP tools must be authorized")
            self.tool_specs = [{"type": "function", "name": tool.name, "description": (tool.description or "")
                                + " The returned receipt_ref is trusted query evidence; cite it as an ARTIFACT when using this result.",
                                "inputSchema": deepcopy(tool.inputSchema)} for tool in tools]
            # Make legal query choices visible without requiring guessed UUIDs
            # or predicate names. Runtime authorization remains authoritative.
            for tool in self.tool_specs:
                if tool["name"] == "nf_query_batch":
                    lookup = tool["inputSchema"]["properties"]["lookups"]["items"]["properties"]
                    lookup["subject_id"]["enum"] = self.scope["query_scope"]["subject_ids"]
                    lookup["predicate"]["enum"] = self.scope["query_scope"]["predicates"]
                    tool["description"] += " Fixed query plane: " + self.scope["query_scope"]["plane"] + "."
                elif tool["name"] == "nf_review_material":
                    tool["description"] += (" A diagnosis report may include frozen finding_provenance: exact historical report/draft"
                                            " versions and matching finding hashes, not a stale/current verdict. Compare all candidates.")
            self._result_limit = self.scope["result_byte_limit"] - len(canonical_bytes(self.tool_specs)) - 4096
            require(self._result_limit > 0, "CAPACITY_LIMIT", "Native tool schemas exceed the reserved input allowance")
            return self
        except BaseException:
            await self._connection.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        return await self._connection.__aexit__(*args)

    def bind(self, thread_id, turn_id):
        require(self.thread_id is None and isinstance(thread_id, str) and thread_id
                and isinstance(turn_id, str) and turn_id, "VIEW_DENIED", "A single trusted native turn is required")
        self.thread_id, self.turn_id = thread_id, turn_id

    def _live(self, repo):
        task, grant, _context, spec = self.runtime._bound_state(repo)
        require(all(task[key] == self.scope[key] for key in
                    ("task_id", "generation", "work_id", "branch_id", "role_id", "principal_id", "grant_id", "context_bundle_id"))
                and grant["epoch"] == self.scope["grant_epoch"] and spec["world_ref"] == self.scope["world_ref"],
                "VIEW_DENIED", "Native tool scope changed")
        return task

    async def call(self, params):
        """Handle an actual app-server item/tool/call through a real MCP ClientSession."""
        name, arguments, call_id = params.get("tool"), params.get("arguments"), params.get("callId")
        require(params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id
                and self.turn_id is not None and params.get("namespace") is None
                and name in self.allowed_tools and isinstance(arguments, dict) and isinstance(call_id, str)
                and 0 < len(call_id) <= 256 and len(canonical_bytes(arguments)) <= 8192,
                "VIEW_DENIED", "Host tool request is outside this turn's read-only ceiling")
        identity = fingerprint([name, arguments])
        if call_id in self._calls:
            previous, result = self._calls[call_id]
            require(previous == identity, "VIEW_DENIED", "Host tool identity was reused with different arguments")
            return result
        require(len(self._calls) < MAX_CALLS, "CAPACITY_LIMIT", "Native review tool-call limit reached")
        # MCP 1.29.0 allocates this integer synchronously at send_request entry.
        # Pin its real protocol id to the existing server audit, including across reconnects.
        request_hash = sha256(str(self._client._request_id).encode()).hexdigest()
        with self.kernel.db.uow(write=False) as repo:
            self._live(repo)
            before = {str(row["event_id"]) for row in _audit_rows(repo, self.scope["task_id"], self.scope["generation"], name, request_hash)}
        result = await self._client.call_tool(name, arguments)
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        # MCP carries the same dictionary both as text content and structured
        # content. Keep the full wire result in evidence; show the business
        # result once so large manuscripts do not consume the input allowance twice.
        model_value = payload.get("structuredContent", payload)
        result_bytes = canonical_bytes(model_value)
        require(len(result_bytes) + 256 + self._total_bytes <= self._result_limit,
                "CAPACITY_LIMIT", "Native review MCP results exceed their bounded input allowance")
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task = self._live(repo)
            rows = [row for row in _audit_rows(repo, self.scope["task_id"], self.scope["generation"], name, request_hash)
                    if str(row["event_id"]) not in before]
            require(len(rows) == 1, "REVIEW_QUERY_REQUIRED", "A unique real MCP invocation is required")
            receipt = {**self.scope, "record_version": 1, "transport": "mcp", "thread_id": self.thread_id,
                "turn_id": self.turn_id, "host_call_id": call_id, "tool": name, "arguments": arguments,
                "arguments_sha256": fingerprint(arguments), "mcp_request_sha256": request_hash,
                "invocation_id": str(rows[0]["event_id"]), "outcome": "ERROR" if result.isError else "OK",
                "result": payload, "result_sha256": fingerprint(payload), "lease_expiry": task["lease_expiry"].isoformat()}
            _audit_pair(repo, receipt)
            ref = _record(self.kernel, repo, self.scope["work_id"], self.runtime.principal.principal_id,
                          self.scope["attempt_id"], "review-mcp:" + fingerprint(call_id), receipt, RECEIPT_KIND)
        response = {"success": not result.isError,
                    "contentItems": [{"type": "inputText", "text": canonical_bytes({"result": model_value, "receipt_ref": ref}).decode()}]}
        self.receipt_refs.append(ref)
        self._calls[call_id] = identity, response
        self._total_bytes += len(canonical_bytes({"result": model_value, "receipt_ref": ref}))
        return response


def require_review_queries(kernel, repo, task, evidence, *, receipt_refs=None):
    """Require successful actual MCP context and state reads by this exact native turn."""
    try:
        return _require_review_queries(kernel, repo, task, evidence, receipt_refs=receipt_refs)
    except (KeyError, TypeError, ValueError, OSError):
        require(False, "REVIEW_QUERY_REQUIRED", "Native MCP evidence is missing or malformed")


def _require_review_queries(kernel, repo, task, evidence, *, receipt_refs):
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"], kind="AGENT_RESPONSE")["payload"]
    metadata = json.loads(response["provider_response_json"])
    refs = metadata.get("review_mcp_receipt_refs")
    require(isinstance(refs, list) and 2 <= len(refs) <= MAX_CALLS
            and (receipt_refs is None or receipt_refs == refs), "REVIEW_QUERY_REQUIRED", "Native review requires actual MCP reads")
    call = repo.one("call_attempts", {"attempt_id": evidence["attempt_id"]})
    host = repo.one("host_turns", {"attempt_id": evidence["attempt_id"]})
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    grant = repo.one("grants", {"grant_id": task["grant_id"]})
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
    blind = bool(job and job["config"].get("review_mcp_blind"))
    material_only = blind or task["role_id"] == "literary_editor"
    allowed_tools, required_tools = (BLIND_TOOLS, BLIND_TOOLS) if material_only else (TOOLS, REQUIRED_TOOLS)
    kernel.authorize(repo, Principal(task["grant_id"], task["principal_id"]), task["work_id"], "READ", task["branch_id"])
    require(task["state"] in {"RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW", "ACCEPTED"}
            and call and host and host["phase"] == "COMPLETED" and grant["active"]
            and metadata.get("codex_thread_id") == host["thread_id"]
            and metadata.get("codex_turn_id") == evidence["provider_receipt"] == response["provider_receipt"] == host["turn_id"]
            and call["request_hash"] == evidence["request_sha256"] == response["request_sha256"]
            and call["task_id"] == evidence["task_id"] == task["task_id"]
            and call["generation"] == evidence["generation"] == task["generation"],
            "REVIEW_QUERY_REQUIRED", "MCP reads are not bound to the completed native review")
    frozen_input = kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    required_materials = {fingerprint(ref) for ref in frozen_input["material_refs"]}
    verified, invocations, successes, read_materials = [], set(), set(), set()
    for ref in refs:
        receipt = kernel.artifact(repo, ref, task["work_id"], kind=RECEIPT_KIND)["payload"]
        binding = kernel.artifact(repo, receipt["binding_ref"], task["work_id"], kind="NATIVE_REVIEW_TOOL_BINDING")["payload"]
        require(all(receipt[key] == task[key] for key in
                    ("work_id", "branch_id", "task_id", "generation", "context_bundle_id", "role_id", "principal_id", "grant_id"))
                and receipt["attempt_id"] == evidence["attempt_id"] and receipt["request_sha256"] == evidence["request_sha256"]
                and receipt["input_ref"] == evidence["input_ref"] and receipt["world_ref"] == spec["world_ref"] == evidence["world_ref"]
                and receipt["thread_id"] == host["thread_id"] and receipt["turn_id"] == host["turn_id"]
                and receipt["grant_epoch"] == grant["epoch"] and receipt["transport"] == "mcp"
                and receipt["tool"] in allowed_tools and receipt["review_mcp_blind"] == binding["review_mcp_blind"] == blind
                and receipt["review_mcp_material_only"] == binding["review_mcp_material_only"] == material_only
                and receipt["arguments_sha256"] == fingerprint(receipt["arguments"])
                and receipt["result_sha256"] == fingerprint(receipt["result"])
                and receipt["invocation_id"] not in invocations
                and receipt["worker_id"] == binding["worker_id"]
                and all(receipt[key] == binding[key] for key in
                        ("attempt_id", "request_sha256", "task_id", "generation", "grant_id", "grant_epoch", "input_ref", "world_ref", "context_bundle_id"))
                and ref["artifact_revision_id"] == str(uuid5(UUID(evidence["attempt_id"]),
                    "review-mcp:" + fingerprint(receipt["host_call_id"]) + ":revision"))
                and (task["lease_owner"] is None or task["lease_owner"] == receipt["worker_id"]),
                "REVIEW_QUERY_REQUIRED", "MCP receipt does not belong to this frozen native review")
        started, _ = _audit_pair(repo, receipt)
        require(call["created_at"] <= started["event_time"], "REVIEW_QUERY_REQUIRED", "MCP read predates its native attempt")
        invocations.add(receipt["invocation_id"])
        if receipt["outcome"] == "OK" and not receipt["result"].get("isError"):
            value = receipt["result"].get("structuredContent", {})
            if receipt["tool"] == "nf_context_get":
                require(value.get("world_ref") == spec["world_ref"] and value.get("context_bundle_id") == task["context_bundle_id"],
                        "REVIEW_QUERY_REQUIRED", "MCP context result belongs to another cut")
                successes.add(receipt["tool"])
            elif (receipt["tool"] == "nf_query_batch" and (
                    value.get("all_items_succeeded") is True and value.get("items") and receipt["arguments"].get("lookups")
                    or task["role_id"] == "reviewer" and task["task_kind"] == "verification"
                    and _completed_registration_query(value, receipt["arguments"]))):
                successes.add(receipt["tool"])
            elif receipt["tool"] == "nf_review_material":
                material_ref = receipt["arguments"].get("artifact_ref")
                require(material_ref in frozen_input["material_refs"], "REVIEW_QUERY_REQUIRED", "MCP material is outside the frozen review inputs")
                require(isinstance(value, dict) and bool(value), "REVIEW_QUERY_REQUIRED", "MCP material result is empty")
                if binding.get("provenance_ref"):
                    require(receipt.get("provenance_ref") == binding["provenance_ref"]
                            and receipt.get("provenance_report_ref") == binding["provenance_report_ref"],
                            "REVIEW_QUERY_REQUIRED", "Finding provenance belongs to another frozen binding")
                    if material_ref == binding["provenance_report_ref"]:
                        provenance_ref = binding["provenance_ref"]
                        provenance = kernel.artifact(repo, provenance_ref, task["work_id"], kind=PROVENANCE_KIND)["payload"]
                        require(provenance_ref["artifact_revision_id"] == str(uuid5(UUID(evidence["attempt_id"]),
                                    "review-finding-provenance:revision"))
                                and fingerprint(provenance) == provenance_ref["sha256"]
                                and all(provenance[key] == binding[key] for key in ("task_id", "generation", "context_bundle_id",
                                    "attempt_id", "request_sha256", "input_ref", "world_ref"))
                                and provenance["report_ref"] == material_ref
                                and value.get("finding_provenance_ref") == provenance_ref
                                and value.get("finding_provenance") == provenance["provenance"],
                                "REVIEW_QUERY_REQUIRED", "Finding metadata was not returned by this bound MCP material read")
                read_materials.add(fingerprint(material_ref))
                if material_only:
                    successes.add("nf_review_material")
        verified.append({**receipt, "receipt_ref": ref})
    require(successes == required_tools and required_materials <= read_materials,
            "REVIEW_QUERY_REQUIRED", "Context, system query and all assigned materials must actually be read through MCP")
    return verified
