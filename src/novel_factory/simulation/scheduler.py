"""Virtual-clock scheduling with the normal Harness adapter and dry_run requests."""

import heapq
import json
from concurrent.futures import BrokenExecutor, Future
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from agent_runtime.protocol import DryRunSpec, ModelCallFailure, ModelRequest
from agent_runtime.providers import Profile
from novel_factory.application.prompt_layout import role_messages
from novel_factory.domain.review_gate import blocking_review_issues
from novel_factory.infrastructure.agent_lifecycle import AgentLifecycleManager
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

SCENARIOS = ("novel", "idle", "normal", "review_return", "review_exhausted", "rate_limit", "timeout",
             "unknown_reconciled", "crash_before_send", "crash_after_send", "invalid_json",
             "truncated", "lost_notification", "duplicate_notification", "duplicate_result",
             "late_result", "lease_expired", "restart", "authorization_revoked", "budget_exhausted", "dependencies",
             "storage_disconnect", "worker_pool_failure", "context_mismatch", "book_review_return", "needs_input",
             "review_questions", "internal_question_reread", "internal_question_strategy",
             "review_mcp_recovery", "review_mcp_exhausted", "verification_unknown",
             "stale_finding_provenance", "world_coverage_recovery", "world_coverage_blocked",
             "multi_chapter_coverage", "coverage_semantic_changed")
COVERAGE_SCENARIOS = {"world_coverage_recovery", "world_coverage_blocked", "multi_chapter_coverage", "coverage_semantic_changed"}
VERSION_SCENARIOS = COVERAGE_SCENARIOS | {"stale_finding_provenance"}
INTERNAL_SCENARIOS = {"internal_question_reread", "internal_question_strategy", "verification_unknown"} | VERSION_SCENARIOS
MCP_SCENARIOS = {"review_mcp_recovery", "review_mcp_exhausted"}
TERMINAL = {"ACCEPTED", "FAILED", "CANCELLED"}
PROGRAM_STAGES = {"marker", "reconcile", "freeze", "continuity", "assemble", "book_reconcile", "book_freeze",
                  "query_verification", "world_coverage", "volume_freeze"}
ROLE_STAGES = {
    "plan": "world_designer", "volume_plan": "volume_director", "scene_plan": "volume_director",
    "roleplay": "character_actor", "adjudicate": "adjudication_advisor", "draft": "writer",
    "epistemic": "reviewer", "facts": "evidence_maintainer", "review": "reviewer",
    "chapter_decision": "chief_editor", "strategy": "chief_editor", "memory": "evidence_maintainer",
    "book_structure": "literary_editor", "book_seam": "literary_editor", "book_cross_volume": "literary_editor",
    "book_revision": "writer", "book_facts": "evidence_maintainer",
    "reader_1": "reviewer", "reader_2": "reviewer", "final_editor": "chief_editor",
    "internal_verification": "reviewer",
}


def uid(value):
    return str(uuid5(NAMESPACE_URL, "novelfactory:dry-run:" + value))


@dataclass
class Job:
    id: str
    stage: str
    book: int
    chapter: int = 0
    revision: int = 0
    dependencies: list[str] = field(default_factory=list)
    dependency_mode: str = "AND"
    state: str = "BLOCKED"
    generation: int = 0
    tries: int = 0
    ready_at: float = 0
    reason: str | None = None
    approved: bool | None = None
    source_task: str | None = None
    query_receipt: dict | None = None
    reading_world: str | None = None


class VirtualPool:
    def __init__(self, simulation):
        self.simulation, self.closed = simulation, False

    def submit(self, function, *args, **kwargs):
        future = Future()
        if self.simulation.scenario == "worker_pool_failure" and "worker_pool_failure" not in self.simulation.used_faults:
            self.simulation.used_faults.add("worker_pool_failure")
            future.set_exception(BrokenExecutor("SIMULATED_WORKER_POOL_FAILURE"))
            return future

        def run():
            if self.closed:
                future.cancel()
                return
            future.set_running_or_notify_cancel()
            try:
                future.set_result(function(*args, **kwargs))
            except Exception as error:  # noqa: BLE001 -- deliver injected failure through the normal Future contract.
                future.set_exception(error)

        self.simulation.schedule(1, run)
        return future

    def shutdown(self, wait=True):
        self.closed = True


class MemoryJournal:
    dry_run = True

    def __init__(self, simulation, job, generation, attempt):
        self.simulation, self.job, self.generation, self.attempt = simulation, job, generation, attempt

    def authorize_send(self, attempt):
        simulation = self.simulation
        job = simulation.jobs[self.job]
        allowed = (attempt == self.attempt and job.generation == self.generation
                   and job.state == "RUNNING" and simulation.scenario != "authorization_revoked")
        if allowed:
            simulation.ledger[attempt]["state"] = "SENT"
            simulation.emit("HARNESS_AUTHORIZED", task=self.job, generation=self.generation, attempt=attempt)
        return allowed

    def record(self, attempt, thread, turn, phase):
        self.simulation.emit("HARNESS_HOST_PHASE", attempt=attempt, phase=phase,
                             simulated_thread=thread, simulated_turn=turn)


class Simulation:
    def __init__(self, *, scenario="novel", seed=7, books=1, chapters=3, workers=2,
                 logical_agents=1000, review_pass_rate=0.7, max_reworks=3, max_steps=100000):
        self.scenario, self.seed = scenario, seed
        self.books, self.chapters, self.workers = books, chapters, workers
        self.logical_agents, self.pass_rate = logical_agents, review_pass_rate
        self.max_reworks, self.max_steps = max_reworks, max_steps
        self.now, self.sequence, self.steps = 0.0, 0, 0
        self.events, self.trace, self.jobs, self.ledger, self.pending = [], [], {}, {}, {}
        self.used_faults, self.acceptances, self.frozen = set(), {}, set()
        self.releases, self.handled_book_reviews = {}, set()
        self.chapter_manifests = {}
        self.world_cuts, self.world_heads, self.volume_manifests = {}, {}, {}
        for book in range(self.books):
            base = {"id": uid(f"world:{book}"), "work": uid(f"work:{book}"), "branch": uid(f"branch:{book}"),
                    "parent": None, "frontier": {"tick": 0, "phase": 0}, "canon": sha256(f"canon:{book}".encode()).hexdigest(),
                    "registry": sha256(f"registry:{book}".encode()).hexdigest(), "interpretation": "DRY_RUN_INTERPRETATION_V1"}
            self.world_cuts[base["id"]] = base
            self.world_heads[book] = base["id"]
        self.late_results = self.duplicate_results = self.max_inflight = 0
        self.wake_delays, self.problems = [], []
        self.next_scan, self.poll_seconds, self.notified = 0.0, 5.0, False
        self.worker_events = []
        self.storage_available = True
        self.coverage_waiting = set()
        self.lifecycle = self.new_lifecycle()
        self._build()

    def new_lifecycle(self):
        return AgentLifecycleManager(lambda: VirtualPool(self), idle_seconds=3,
                                     clock=lambda: self.now, observe=self.worker_events.append)

    def emit(self, event, **data):
        self.trace.append({"at": self.now, "event": event, **data})

    def schedule(self, delay, action):
        self.sequence += 1
        heapq.heappush(self.events, (self.now + delay, self.sequence, action))

    def add(self, stage, book, chapter=0, revision=0, dependencies=(), state=None, *, tag=None, source_task=None):
        key = f"{book}:{chapter}:{stage}:{revision}"
        identity = uid(key if tag is None else key + ":" + tag)
        job = Job(identity, stage, book, chapter, revision, list(dependencies),
                  state=state or ("BLOCKED" if dependencies else "READY"), ready_at=self.now)
        self.jobs[identity] = job
        job.source_task = source_task
        return job

    def _build(self):
        if self.scenario == "idle":
            return
        if self.scenario == "storage_disconnect":
            self.storage_available = False
            def reconnect():
                self.storage_available = True
                self.emit("STORAGE_RECONNECTED")
                self._wake()
            self.schedule(2, reconnect)
        if self.scenario == "dependencies":
            failed, accepted = self.add("marker", 0, state="FAILED"), self.add("marker", 1, state="ACCEPTED")
            self.add("marker", 2, dependencies=(failed.id, accepted.id))
            self.add("marker", 3, dependencies=(failed.id, accepted.id)).dependency_mode = "OR"
            return
        for book in range(self.books):
            previous = self.add("plan", book)
            previous = self.add("volume_plan", book, dependencies=(previous.id,))
            previous = self.add("scene_plan", book, dependencies=(previous.id,))
            chapter_freezes = []
            for chapter in range(self.chapters):
                actor = self.add("roleplay", book, chapter, dependencies=(previous.id,))
                adjudicated = self.add("adjudicate", book, chapter, dependencies=(actor.id,))
                decision = self._chapter_candidate(book, chapter, 0, adjudicated.id)
                frozen = self.add("freeze", book, chapter, dependencies=(decision.id,))
                chapter_freezes.append(frozen.id)
                memory = self.add("memory", book, chapter, dependencies=(frozen.id,))
                previous = self.add("continuity", book, chapter, dependencies=(memory.id,))
            volume = self.add("volume_freeze", book, dependencies=(*chapter_freezes, previous.id))
            assembled = self.add("assemble", book, dependencies=(volume.id,))
            editor = self._book_edition(book, 0, assembled.id)
            self.add("book_freeze", book, dependencies=(editor.id,))
        if self.scenario == "lost_notification":
            for job in self.jobs.values():
                if job.stage == "plan":
                    job.state, job.reason = "BLOCKED", "EXTERNAL_READY"
            def ready():
                for job in self.jobs.values():
                    if job.stage == "plan":
                        job.state, job.reason, job.ready_at = "READY", None, self.now
                self.emit("NOTIFICATION_DROPPED")
            self.schedule(1, ready)

    def _chapter_candidate(self, book, chapter, revision, predecessor, *, reading_world=None):
        draft = self.add("draft", book, chapter, revision, dependencies=(predecessor,))
        knowledge = self.add("epistemic", book, chapter, revision, dependencies=(draft.id,))
        facts = self.add("facts", book, chapter, revision, dependencies=(draft.id, knowledge.id))
        reconciled = self.add("reconcile", book, chapter, revision, dependencies=(facts.id,))
        review = self.add("review", book, chapter, revision, dependencies=(draft.id, reconciled.id))
        for reading in (knowledge, facts, reconciled, review):
            reading.reading_world = reading_world
        return self.add("chapter_decision", book, chapter, revision, dependencies=(review.id, reconciled.id))

    def _book_edition(self, book, edition, predecessor):
        structure = self.add("book_structure", book, revision=edition, dependencies=(predecessor,))
        seam = self.add("book_seam", book, revision=edition, dependencies=(structure.id,))
        cross = self.add("book_cross_volume", book, revision=edition, dependencies=(seam.id,))
        readers = [self.add(stage, book, revision=edition, dependencies=(cross.id,)) for stage in ("reader_1", "reader_2")]
        return self.add("final_editor", book, revision=edition, dependencies=tuple(job.id for job in readers))

    def _subject(self, job):
        frozen = self.chapter_manifests.get((job.book, job.chapter))
        revision = frozen["revision"] if job.stage == "memory" and frozen else job.revision
        source = (f"book-edition:{job.book}:{job.revision}" if job.stage.startswith("book_")
                  or job.stage in {"reader_1", "reader_2", "final_editor"}
                  else f"chapter-draft:{job.book}:{job.chapter}:{revision}")
        return {"artifact_id": uid(source), "artifact_revision_id": uid(source + ":revision"),
                "sha256": sha256(source.encode()).hexdigest()}

    def _wake(self):
        if self.scenario == "duplicate_notification":
            self.emit("DUPLICATE_WAKE_HINTS", count=5)
        # A boolean coalesces notifications; durable eligibility remains authoritative.
        self.notified = self.scenario != "lost_notification"

    def _ready(self):
        for job in self.jobs.values():
            if job.state != "BLOCKED" or job.reason in {"EXTERNAL_READY", "RETRY_BACKOFF"}:
                continue
            parents = [self.jobs[key].state for key in job.dependencies]
            good = [state == "ACCEPTED" for state in parents]
            if (all(good) if job.dependency_mode == "AND" else any(good)):
                job.state, job.ready_at = "READY", self.now
            elif (any(state in {"FAILED", "CANCELLED"} for state in parents)
                  if job.dependency_mode == "AND" else all(state in TERMINAL for state in parents)):
                job.state, job.reason = "FAILED", "PREREQUISITE_CLOSED"

    def _outcome(self, job):
        fault_stage = job.stage == "draft"
        fault = self.scenario
        question_fault = f"internal_question:{job.book}:{job.chapter}" if fault == "multi_chapter_coverage" else "internal_question"
        if fault in INTERNAL_SCENARIOS and job.stage == "review" and question_fault not in self.used_faults:
            self.used_faults.update({"internal_question", question_fault})
            self.emit("FAULT_INJECTED", fault="internal_question", task=job.id)
            return "review_questions"
        if fault == "verification_unknown" and job.stage == "internal_verification":
            return "timeout"
        if fault == "review_questions" and job.stage == "review" and fault not in self.used_faults:
            self.used_faults.add(fault)
            self.emit("FAULT_INJECTED", fault=fault, task=job.id)
            return fault
        if (fault_stage and fault not in self.used_faults
                and fault in {"rate_limit", "timeout", "crash_before_send", "crash_after_send", "invalid_json", "truncated", "unknown_reconciled", "context_mismatch", "needs_input"}):
            self.used_faults.add(fault)
            self.emit("FAULT_INJECTED", fault=fault, task=job.id)
            return "timeout" if fault == "unknown_reconciled" else fault
        return "normal"

    def _invoke(self, job_id, generation, attempt, outcome):
        job = self.jobs[job_id]
        role = ROLE_STAGES[job.stage]
        identity = {"schema_version": "2.1", "task_id": job.id, "generation": generation,
                    "context_bundle_id": uid(f"{job.id}:{generation}:context"), "role_id": role,
                    "actor_id": uid(f"actor:{job.book}") if role == "character_actor" else None,
                    "world_ref": {"work_id": uid(f"work:{job.book}"), "branch_id": uid(f"branch:{job.book}"),
                                  "world_revision_id": job.reading_world or uid(f"world:{job.book}")}}
        messages = role_messages("DRY_RUN: isolated role, simulated JSON output only.",
            {"assignment": "Simulate the assigned stage", "context": identity,
             "dry_run_subject_ref": self._subject(job),
             "knowledge_cut": {"plane": "BELIEF" if role == "character_actor" else "WORLD"}}, identity)
        rate = 1 if job.stage in {"epistemic", "internal_verification"} else self.pass_rate
        if job.stage == "reader_1" and self.scenario == "book_review_return" and "book_review_return" not in self.used_faults:
            rate = 0
            self.used_faults.add("book_review_return")
        if job.stage == "review" and (self.scenario == "review_exhausted"
                                      or self.scenario == "review_return" and "review_return" not in self.used_faults):
            rate = 0
            self.used_faults.add("review_return")
        payload = ({"summary": "DRY_RUN", "issues": [], "counterinterpretations": [], "evidence_refs": []}
                   if role == "reviewer" else
                   {"text": "DRY_RUN_PLACEHOLDER", "event_refs": [], "important_deltas": [], "evidence_refs": []})
        if role == "chief_editor":
            payload = {"decision": "DRY_RUN_CHANGE_METHOD" if job.stage == "strategy" else "DRY_RUN_APPROVE",
                       "reason": "DRY_RUN_POLICY_DECISION", "deferred_questions": [], "evidence_refs": []}
        if job.stage == "internal_verification" and self.scenario in VERSION_SCENARIOS:
            payload["verification_status"] = self.jobs[job.dependencies[0]].query_receipt["result"]
        if job.stage == "facts":
            payload["world_observation"] = self._factual_observation(job)
        spec = DryRunSpec(self.seed, rate, outcome, payload)
        simple = role in {"writer", "character_actor"} or job.stage == "memory"
        maximum = 8192 if simple else 262144
        profile = (Profile("deepseek", "deepseek-flash", "DRY_RUN_NO_CREDENTIAL", maximum)
                   if simple else Profile("codex-cli", "gpt-6.1-sol", "DRY_RUN_NO_CREDENTIAL", maximum,
                                                     auth_mode="host-login", model_output_ceiling=maximum))
        digest = sha256(json.dumps({"messages": messages, "dry_run": True, "simulation": asdict(spec)},
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        request = ModelRequest(messages, digest, maximum, attempt, dry_run=True, simulation=spec)
        self.ledger[attempt]["request_sha256"] = digest
        self.ledger[attempt]["identity"] = identity
        self.emit("HARNESS_REQUEST", task=job.id, generation=generation, attempt=attempt,
                  dry_run=request.dry_run, adapter=profile.kind, model=profile.model, outcome=outcome,
                  stage=job.stage, source_ref=self._subject(job),
                  logical_agent_id=uid(f"agent:{job.book}:{job.stage if job.stage in {'reader_1','reader_2','internal_verification'} else role}"))
        # The normal production adapter forwards dry_run to the normal provider
        # dispatcher. Only its final execution backend is substituted.
        adapter = RuntimeModelAdapter(profile, MemoryJournal(self, job.id, generation, attempt))
        adapter.check_available(dry_run=True)
        response = adapter.generate(request)
        if not response.dry_run:
            raise AssertionError("Dry-run Harness returned a live response")
        if job.stage in {"review", "internal_verification"} and self.scenario in INTERNAL_SCENARIOS | MCP_SCENARIOS:
            missing = (job.stage == "review" and self.scenario in MCP_SCENARIOS
                       and (self.scenario == "review_mcp_exhausted" or job.tries < 3))
            if not missing:
                receipt = {"transport": "DRY_RUN_MEMORY", "task": job.id, "generation": generation,
                    "request_sha256": digest, "source_ref": self._subject(job),
                    "tools": ["nf_context_get", "nf_query_batch", "nf_review_material"]}
                receipt["sha256"] = self._receipt_hash(receipt)
                response.raw_response["simulated_review_mcp_receipt"] = receipt
                self.emit("SIMULATED_REVIEW_MCP_READS", task=job.id, generation=generation,
                          source_ref=self._subject(job), receipt_sha256=receipt["sha256"])
        return response

    @staticmethod
    def _receipt_hash(receipt):
        return sha256(json.dumps({key: value for key, value in receipt.items() if key != "sha256"},
                                 sort_keys=True).encode()).hexdigest()

    def _valid_review_receipt(self, job, generation, call, response):
        receipt = response.raw_response.get("simulated_review_mcp_receipt")
        return (isinstance(receipt, dict) and receipt.get("transport") == "DRY_RUN_MEMORY"
                and receipt.get("task") == job.id and receipt.get("generation") == generation
                and receipt.get("request_sha256") == call["request_sha256"]
                and receipt.get("source_ref") == self._subject(job)
                and receipt.get("tools") == ["nf_context_get", "nf_query_batch", "nf_review_material"]
                and receipt.get("sha256") == self._receipt_hash(receipt))

    def _dispatch(self, job):
        if job.stage in PROGRAM_STAGES:
            if job.stage in {"volume_freeze", "assemble"} and not self._volume_gate(job):
                return
            if job.stage == "world_coverage":
                self._coverage(job)
                return
            if job.stage == "query_verification":
                source = self.jobs[job.source_task]
                if (source.state != "PAUSED" or source.reason != "INTERNAL_QUESTION_PENDING"
                        or self._subject(source) != self._subject(job)):
                    job.state, job.reason = "FAILED", "VERIFICATION_SOURCE_MISMATCH"
                    self.problems.append(job.reason)
                    return
                outcome = ("PENDING_EVALUATION" if self.scenario in COVERAGE_SCENARIOS and not job.reading_world else
                           "STALE_REPORT" if self.scenario == "stale_finding_provenance" else
                           "CONTRADICTORY" if self.scenario == "internal_question_strategy" else "CONSISTENT")
                receipt = {"transport": "DRY_RUN_MEMORY", "source_task": source.id,
                           "source_ref": self._subject(source), "query_task": job.id,
                           "reading_world": job.reading_world or uid(f"world:{job.book}"), "result": outcome}
                receipt["state_answer"] = {"value_status": "UNRESOLVED" if outcome == "PENDING_EVALUATION" else "VALUE",
                    "evaluation_status": "PENDING" if outcome == "PENDING_EVALUATION" else "EVALUATED",
                    "knowledge_status": "UNKNOWN" if outcome == "PENDING_EVALUATION" else "KNOWN"}
                if outcome == "STALE_REPORT":
                    receipt["finding_provenance"] = {"current_source_ref": self._subject(source),
                        "matched_report_source_ref": {**self._subject(source),
                            "artifact_revision_id": uid("prior-draft:" + source.id),
                            "sha256": sha256(("prior-draft:" + source.id).encode()).hexdigest()},
                        "same_version": False, "transport": "DRY_RUN_MEMORY"}
                    self.emit("SIMULATED_FINDING_PROVENANCE_READ", task=job.id,
                              current_source_ref=self._subject(source), old_report_same_version=False)
                receipt["sha256"] = self._receipt_hash(receipt)
                job.query_receipt = receipt
                self.emit("SIMULATED_QUERY_VERIFICATION", task=job.id, source_task=source.id,
                          source_ref=receipt["source_ref"], result=receipt["result"], receipt_sha256=receipt["sha256"])
            if job.stage == "freeze":
                decision = self.jobs[job.dependencies[0]]
                reviews = [self.jobs[key] for key in decision.dependencies if self.jobs[key].stage == "review"]
                if decision.state != "ACCEPTED" or len(reviews) != 1 or reviews[0].approved is not True:
                    job.state, job.reason = "FAILED", "CHAPTER_FREEZE_GATE_REJECTED"
                    self.problems.append(job.reason)
                    return
                reconciled = next(self.jobs[key] for key in decision.dependencies if self.jobs[key].stage == "reconcile")
                factual = self.jobs[reconciled.dependencies[0]]
                draft = next(self.jobs[key] for key in factual.dependencies if self.jobs[key].stage == "draft")
                knowledge = next(self.jobs[key] for key in factual.dependencies if self.jobs[key].stage == "epistemic")
                if factual.reading_world != reviews[0].reading_world or knowledge.reading_world != factual.reading_world:
                    job.state, job.reason = "FAILED", "READING_WORLD_CUT_MISMATCH"
                    self.problems.append(job.reason)
                    return
            if job.stage == "book_freeze":
                editor = self.jobs[job.dependencies[0]]
                readers = [self.jobs[key] for key in editor.dependencies]
                if (editor.stage != "final_editor" or editor.book != job.book
                        or editor.state != "ACCEPTED" or editor.approved is not True or len(readers) != 2
                        or {row.stage for row in readers} != {"reader_1", "reader_2"}
                        or any(row.state != "ACCEPTED" or row.approved is not True for row in readers)
                        or any(row.book != editor.book or row.revision != editor.revision
                               or self._subject(row) != self._subject(editor) for row in readers)
                        or any((job.book, chapter) not in self.frozen for chapter in range(self.chapters))):
                    job.state, job.reason = "FAILED", "BOOK_FREEZE_GATE_REJECTED"
                    self.problems.append(job.reason)
                    return
            self._accept(job)
            if job.stage == "freeze":
                self.frozen.add((job.book, job.chapter))
                revision = reviews[0].revision
                self.chapter_manifests[job.book, job.chapter] = {
                    "book": job.book, "chapter": job.chapter, "revision": revision,
                    "draft_task": draft.id, "facts_task": factual.id,
                    "reconciliation_task": reconciled.id,
                    "review_task": reviews[0].id, "editor_task": decision.id,
                    "reading_world": factual.reading_world or uid(f"world:{job.book}"),
                    "source_world": uid(f"world:{job.book}"), "draft_ref": self._subject(draft),
                    "factual_observation_sha256": factual.query_receipt["sha256"],
                    "scene_ref": uid(f"scene:{job.book}:{job.chapter}"), "batch_ref": uid(f"batch:{job.book}"),
                    "plan_ref": uid(f"plan:{job.book}")}
                manifest = self.chapter_manifests[job.book, job.chapter]
                manifest["sha256"] = self._receipt_hash(manifest)
                self.emit("CHAPTER_FROZEN", book=job.book, chapter=job.chapter)
            if job.stage == "book_freeze":
                editor = self.jobs[job.dependencies[0]]
                readers = [self.jobs[key] for key in editor.dependencies]
                manifest = {"book": job.book, "status": "FROZEN", "release_id": uid(f"release:{job.book}"),
                    "edition": editor.revision, "chapters": self.chapters,
                    "chapter_task_ids": [uid(f"{job.book}:{chapter}:freeze:0") for chapter in range(self.chapters)],
                    "chapter_manifests": [self.chapter_manifests[job.book, chapter] for chapter in range(self.chapters)],
                    "independent_readers": [row.id for row in readers], "editor_task_id": editor.id,
                    "reviewed_edition_ref": self._subject(editor)}
                self.releases[job.book] = {**manifest, "manifest_sha256": sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
                self.emit("BOOK_RELEASE_FROZEN", **self.releases[job.book])
            return
        if job.stage == "draft" and job.chapter and not self._predecessor_gate(job):
            return
        if ((job.stage.startswith("book_") or job.stage in {"reader_1", "reader_2", "final_editor"})
                and not self._editing_gate(job)):
            return
        if self.scenario == "budget_exhausted" and job.stage == "draft":
            job.state, job.reason = "PAUSED", "BUDGET_EXCEEDED"
            self.emit("BUDGET_HOLD", task=job.id)
            return
        job.state, job.generation, job.tries = "RUNNING", job.generation + 1, job.tries + 1
        self.wake_delays.append(self.now - job.ready_at)
        attempt = uid(f"{job.id}:{job.generation}:attempt")
        self.ledger[attempt] = {"task": job.id, "generation": job.generation, "state": "PREPARED", "reserved": 1}
        outcome = self._outcome(job)
        future = self.lifecycle.submit(self._invoke, job.id, job.generation, attempt, outcome)
        self.pending[future] = (job.id, job.generation, attempt)
        self.max_inflight = max(self.max_inflight, len(self.pending))

    def _pure_coverage_chain(self, source, target):
        """Synthetic counterpart of scoped formal SCHEDULE_ADVANCE compatibility.

        Keep original cuts in every source receipt. Matching hashes alone do
        not prove ancestry, registration, interpretation or accepted closures.
        """
        original = self.world_cuts.get(source)
        if original is None:
            return False
        seen = set()
        while target != source:
            current = self.world_cuts.get(target)
            if not current or target in seen or len(seen) >= len(self.world_cuts):
                return False
            seen.add(target)
            parent = self.world_cuts.get(current.get("parent"))
            proof = current.get("closure")
            if (not parent or not isinstance(proof, dict) or proof.get("sha256") != self._receipt_hash(proof)
                    or proof.get("transport") != "DRY_RUN_MEMORY" or proof.get("template") != "SCHEDULE_ADVANCE"
                    or proof.get("accepted") is not True or proof.get("event_refs") != []
                    or proof.get("from_world") != parent["id"] or proof.get("to_world") != current["id"]
                    or proof.get("target_frontier") != current["frontier"]
                    or (parent["frontier"]["tick"], parent["frontier"]["phase"]) >=
                       (current["frontier"]["tick"], current["frontier"]["phase"])
                    or any(proof.get(key) != [] for key in ("event_candidates", "event_operations", "record_operations", "trigger_evaluations"))
                    or any(current[key] != parent[key] or current[key] != original[key]
                           for key in ("work", "branch", "canon", "registry", "interpretation"))):
                return False
            target = current["parent"]
        return True

    def _factual_observation(self, job):
        world = job.reading_world or uid(f"world:{job.book}")
        cut = self.world_cuts[world]
        receipt = {"transport": "DRY_RUN_MEMORY", "source_ref": self._subject(job), "reading_world": world,
                   "value_status": "VALUE", "evaluation_status": "EVALUATED", "knowledge_status": "KNOWN",
                   "canon": cut["canon"], "registry": cut["registry"], "interpretation": cut["interpretation"]}
        receipt["sha256"] = self._receipt_hash(receipt)
        return receipt

    def _chapter_proof(self, book, chapter, target):
        manifest = self.chapter_manifests.get((book, chapter))
        if not manifest or manifest.get("sha256") != self._receipt_hash(manifest):
            return False
        draft = self.jobs.get(manifest.get("draft_task"))
        facts = self.jobs.get(manifest.get("facts_task"))
        review = self.jobs.get(manifest.get("review_task"))
        reconciled = self.jobs.get(manifest.get("reconciliation_task"))
        editor = self.jobs.get(manifest.get("editor_task"))
        return (manifest["book"] == book and manifest["chapter"] == chapter
                and manifest["source_world"] == uid(f"world:{book}")
                and manifest["scene_ref"] == uid(f"scene:{book}:{chapter}")
                and manifest["batch_ref"] == uid(f"batch:{book}") and manifest["plan_ref"] == uid(f"plan:{book}")
                and draft is not None and draft.state == "ACCEPTED" and self._subject(draft) == manifest["draft_ref"]
                and draft.book == book and draft.chapter == chapter and draft.revision == manifest["revision"]
                and facts is not None and facts.state == "ACCEPTED" and facts.book == book and facts.chapter == chapter
                and facts.revision == manifest["revision"] and draft.id in facts.dependencies
                and facts.query_receipt == self._factual_observation(facts)
                and manifest.get("factual_observation_sha256") == facts.query_receipt["sha256"]
                and review is not None and review.state == "ACCEPTED" and review.approved is True
                and review.book == book and review.chapter == chapter and review.revision == manifest["revision"]
                and reconciled is not None and reconciled.state == "ACCEPTED" and reconciled.dependencies == [facts.id]
                and draft.id in review.dependencies and reconciled.id in review.dependencies
                and editor is not None and editor.state == "ACCEPTED" and editor.book == book and editor.chapter == chapter
                and editor.revision == manifest["revision"] and review.id in editor.dependencies and reconciled.id in editor.dependencies
                and facts.reading_world == review.reading_world
                and (facts.reading_world or uid(f"world:{book}")) == manifest["reading_world"]
                and self._pure_coverage_chain(manifest["source_world"], manifest["reading_world"])
                and self._pure_coverage_chain(manifest["reading_world"], target))

    def _predecessor_gate(self, job):
        previous = self.chapter_manifests.get((job.book, job.chapter - 1))
        if not previous or not self._chapter_proof(job.book, job.chapter - 1, previous["reading_world"]):
            job.state, job.reason = "PAUSED", "PREDECESSOR_SOURCE_CUT_MISMATCH"
            self.emit("PREDECESSOR_HANDOFF_REJECTED", task=job.id, reason=job.reason)
            return False
        self.emit("PREDECESSOR_HANDOFF_PROVEN", task=job.id, source_world=uid(f"world:{job.book}"),
                  predecessor_world=previous["reading_world"], predecessor_draft_ref=previous["draft_ref"],
                  compatibility="SCOPED_PURE_COVERAGE_CHAIN")
        return True

    def _volume_gate(self, job):
        target = self.world_heads[job.book]
        chapters = [self.chapter_manifests.get((job.book, chapter)) for chapter in range(self.chapters)]
        if (any(item is None for item in chapters)
                or not all(self._chapter_proof(job.book, chapter, target) for chapter in range(self.chapters))):
            job.state, job.reason = "PAUSED", "VOLUME_SOURCE_CUT_MISMATCH"
            self.emit("MULTI_CHAPTER_FREEZE_REJECTED", task=job.id, reason=job.reason)
            return False
        if job.stage == "volume_freeze":
            receipt = {"transport": "DRY_RUN_MEMORY", "book": job.book, "adopted_world": target,
                "chapters": [{key: item[key] for key in ("chapter", "source_world", "reading_world", "draft_ref", "sha256")}
                             for item in chapters]}
            receipt["sha256"] = self._receipt_hash(receipt)
            self.volume_manifests[job.book] = receipt
            self.emit("MULTI_CHAPTER_FREEZE_PROVEN", task=job.id, adopted_world=target,
                      original_reading_worlds=[item["reading_world"] for item in chapters])
        return True

    def _editing_gate(self, job):
        volume = self.volume_manifests.get(job.book)
        if (not volume or volume.get("sha256") != self._receipt_hash(volume)
                or len(volume.get("chapters", [])) != self.chapters
                or not self._pure_coverage_chain(volume["adopted_world"], self.world_heads[job.book])
                or any(not self._chapter_proof(job.book, chapter, volume["adopted_world"])
                       for chapter in range(self.chapters))):
            job.state, job.reason = "PAUSED", "FULL_VOLUME_SOURCE_CUT_MISMATCH"
            self.emit("FULL_VOLUME_EDITING_REJECTED", task=job.id, reason=job.reason)
            return False
        job.reading_world = volume["adopted_world"]
        self.emit("FULL_VOLUME_SOURCE_CUTS_PROVEN", task=job.id, adopted_world=job.reading_world,
                  source_worlds=[item["source_world"] for item in volume["chapters"]],
                  reading_worlds=[item["reading_world"] for item in volume["chapters"]], edition=job.revision)
        return True

    def _accept(self, job):
        if job.state == "ACCEPTED":
            self.duplicate_results += 1
            return
        job.state, job.reason = "ACCEPTED", None
        self.acceptances[job.id] = self.acceptances.get(job.id, 0) + 1
        self.emit("TASK_ACCEPTED", task=job.id, generation=job.generation)
        self._wake()

    def _coverage(self, job):
        source = self.jobs[job.source_task]
        verifier = self.jobs[job.dependencies[0]]
        query = self.jobs[verifier.dependencies[0]]
        if (verifier.state != "ACCEPTED" or query.query_receipt["result"] != "PENDING_EVALUATION"
                or not self._valid_internal_query(verifier)):
            job.state, job.reason = "FAILED", "COVERAGE_QUERY_PROOF_REQUIRED"
            self.problems.append(job.reason)
            return
        if self.scenario == "world_coverage_blocked" and "world_process_wait" not in self.used_faults:
            self.used_faults.add("world_process_wait")
            self.coverage_waiting.add(job.id)
            job.state, job.reason = "PAUSED", "WAITING_WORLD_PROCESS"
            self.emit("WORLD_PROCESS_WAIT", task=job.id, source_ref=self._subject(source))
            def completed():
                self.coverage_waiting.remove(job.id)
                job.state, job.reason, job.ready_at = "READY", None, self.now
                self.emit("REGISTERED_WORLD_PROCESS_COMPLETED", task=job.id)
                self._wake()
            self.schedule(8, completed)
            return
        if self.coverage_waiting:
            self.problems.append("COVERAGE_ADVANCED_OVER_WORLD_BLOCKER")
            return
        source_head = self.world_heads[job.book]
        if self.scenario == "coverage_semantic_changed":
            changed = {**self.world_cuts[source_head], "id": uid("changed-canon:" + source.id),
                       "parent": source_head, "canon": sha256(b"EXTERNAL_CANON_CHANGE").hexdigest(), "closure": None}
            self.world_cuts[changed["id"]] = changed
            self.world_heads[job.book] = changed["id"]
            source_head = changed["id"]
        if not self._pure_coverage_chain(query.query_receipt["reading_world"], source_head):
            job.state, job.reason = "PAUSED", "WAITING_WORLD_REVALIDATION"
            self.emit("SEMANTIC_WORLD_CHANGE_REJECTED", task=job.id, reason=job.reason)
            return
        job.reading_world = uid("coverage-world:" + source.id)
        target = {"tick": job.chapter + 1, "phase": 1}
        proof = {"transport": "DRY_RUN_MEMORY", "from_world": source_head, "to_world": job.reading_world,
                 "template": "SCHEDULE_ADVANCE", "target_frontier": target,
                 "accepted": True, "event_refs": [], "event_candidates": [], "event_operations": [],
                 "record_operations": [], "trigger_evaluations": []}
        proof["sha256"] = self._receipt_hash(proof)
        self.world_cuts[job.reading_world] = {**self.world_cuts[source_head], "id": job.reading_world,
            "parent": source_head, "frontier": target, "closure": proof}
        self.world_heads[job.book] = job.reading_world
        receipt = {"transport": "DRY_RUN_MEMORY", "source_task": source.id,
            "source_ref": self._subject(source), "from_world": source_head,
            "to_world": job.reading_world, "canonical_effects_applied": 0, "allowance_changed": False}
        receipt["sha256"] = self._receipt_hash(receipt)
        job.query_receipt = receipt
        self.emit("SIMULATED_FORMAL_COVERAGE_ACCEPTED", task=job.id, **receipt)
        self._accept(job)
        followup = self.add("query_verification", source.book, source.chapter, source.revision,
            dependencies=(job.id,), tag="coverage-child", source_task=source.id)
        followup.reading_world = job.reading_world
        child = self.add("internal_verification", source.book, source.chapter, source.revision,
            dependencies=(followup.id,), tag="coverage-child", source_task=source.id)
        child.reading_world = job.reading_world
        self.emit("COVERAGE_CHILD_ENROLLED", task=child.id, parent_task=verifier.id,
                  source_ref=self._subject(source), reading_world=job.reading_world)

    def _retry(self, job, reason):
        if job.tries >= 3:
            job.state, job.reason = "FAILED", reason
            self._wake()
            return
        job.state, job.reason = "BLOCKED", "RETRY_BACKOFF"
        delay = min(16, 2 ** job.tries)
        def ready():
            job.state, job.reason, job.ready_at = "READY", None, self.now
            self._wake()
        self.schedule(delay, ready)
        self.emit("RETRY_SCHEDULED", task=job.id, reason=reason, after=delay)

    def _deliver(self, job, generation, attempt, response):
        call = self.ledger[attempt]
        if self.scenario in {"late_result", "lease_expired"} and job.stage == "draft" and "late_result" not in self.used_faults:
            self.used_faults.add("late_result")
            job.generation += 1
            job.state = "READY"
            self.emit("LEASE_REPLACED", task=job.id, generation=job.generation)
        if job.generation != generation:
            self.late_results += 1
            call["state"], call["reserved"] = "SUCCEEDED", 0
            self.emit("LATE_RESULT_REJECTED", task=job.id, generation=generation)
            return
        envelope = json.loads(response.text)
        if (response.request_sha256 != call["request_sha256"]
                or any(envelope[key] != value for key, value in call["identity"].items())):
            call["state"], call["reserved"] = "FAILED", 0
            self.emit("CONTEXT_MISMATCH_REJECTED", task=job.id)
            self._retry(job, "RESPONSE_CONTEXT_BINDING_MISMATCH")
            return
        call["state"], call["reserved"] = "SUCCEEDED", 0
        self.emit("HARNESS_RESPONSE", task=job.id, dry_run=response.dry_run,
                  approved=response.raw_response["review_approved"], result_status=envelope["status"])
        if envelope["status"] == "NEEDS_INPUT":
            job.state, job.reason, job.approved = "PAUSED", "INPUT_REQUIRED", None
            self.emit("INPUT_REQUIRED", task=job.id, generation=generation, attempt=attempt,
                      unresolved_question_count=len(envelope["unresolved_questions"]))
            self._wake()
            return
        if envelope["unresolved_questions"]:
            # Production delivery requires a separate decision even if the
            # outer model response claims success and its review has no issues.
            job.state, job.reason, job.approved = "PAUSED", "REVIEW_REQUIRED", None
            call["unresolved_questions"] = list(envelope["unresolved_questions"])
            self.emit("REVIEW_REQUIRED", task=job.id, generation=generation, attempt=attempt,
                      unresolved_questions=list(envelope["unresolved_questions"]))
            if self.scenario in INTERNAL_SCENARIOS:
                job.reason = "INTERNAL_QUESTION_PENDING"
                query = self.add("query_verification", job.book, job.chapter, job.revision,
                    dependencies=(uid(f"{job.book}:{job.chapter}:draft:{job.revision}"),), source_task=job.id)
                verifier = self.add("internal_verification", job.book, job.chapter, job.revision,
                    dependencies=(query.id,), source_task=job.id)
                self.emit("INTERNAL_QUESTION_ENROLLED", task=job.id, query_task=query.id, verification_task=verifier.id)
            self._wake()
            return
        if (job.stage in {"review", "internal_verification"} and self.scenario in INTERNAL_SCENARIOS | MCP_SCENARIOS
                and not self._valid_review_receipt(job, generation, call, response)):
            job.approved = None
            self.emit("REVIEW_MCP_EVIDENCE_MISSING", task=job.id, generation=generation, attempt=attempt)
            if job.tries < 3:
                self._retry(job, "REVIEW_QUERY_REQUIRED")
                self.emit("REVIEW_MCP_REREAD_ENROLLED", task=job.id, reread=job.tries)
            else:
                job.state, job.reason = "PAUSED", "REVIEW_QUERY_RETRIES_EXHAUSTED"
                self.emit("REVIEW_MCP_HOLD", task=job.id, attempt=attempt)
            return
        if job.stage == "internal_verification" and not self._valid_internal_query(job):
            job.state, job.reason = "PAUSED", "VERIFICATION_EVIDENCE_REQUIRED"
            self.problems.append("INTERNAL_RESOLUTION_WITHOUT_QUERY_PROOF")
            return
        if job.stage == "facts":
            observation = envelope["payload"].get("world_observation")
            if observation != self._factual_observation(job):
                job.state, job.reason = "PAUSED", "FACT_WORLD_EVIDENCE_UNRESOLVED"
                self.emit("FACT_WORLD_EVIDENCE_REJECTED", task=job.id, source_ref=self._subject(job))
                self._wake()
                return
            job.query_receipt = observation
        if (job.stage == "internal_verification" and self.scenario in VERSION_SCENARIOS
                and envelope["payload"].get("verification_status") != self.jobs[job.dependencies[0]].query_receipt["result"]):
            job.state, job.reason = "PAUSED", "VERIFICATION_EVIDENCE_REQUIRED"
            self.problems.append("INTERNAL_RESOLUTION_STATUS_MISMATCH")
            return
        role = ROLE_STAGES[job.stage]
        job.approved = (not blocking_review_issues(envelope["payload"]["issues"]) if role == "reviewer" else
                        envelope["payload"].get("decision") == "DRY_RUN_APPROVE" if role == "chief_editor" else None)
        self._accept(job)
        if job.stage == "internal_verification":
            self._continue_internal_question(job)
        elif job.stage == "review" and not job.approved:
            self._return_chapter(job)
        elif job.stage == "strategy":
            decision = self._chapter_candidate(job.book, job.chapter, job.revision, job.id)
            self.jobs[uid(f"{job.book}:{job.chapter}:freeze:0")].dependencies = [decision.id]
        elif job.stage in {"reader_1", "reader_2"}:
            self._book_review(job)
        if self.scenario == "duplicate_result":
            self._accept(job)

    def _valid_internal_query(self, verifier):
        source = self.jobs[verifier.source_task]
        query = self.jobs[verifier.dependencies[0]]
        receipt = query.query_receipt
        if isinstance(receipt, dict):
            pending = receipt.get("result") == "PENDING_EVALUATION"
            if receipt.get("state_answer") != {"value_status": "UNRESOLVED" if pending else "VALUE",
                "evaluation_status": "PENDING" if pending else "EVALUATED", "knowledge_status": "UNKNOWN" if pending else "KNOWN"}:
                return False
        if isinstance(receipt, dict) and receipt.get("result") == "STALE_REPORT":
            provenance = receipt.get("finding_provenance")
            if (not isinstance(provenance, dict) or provenance.get("transport") != "DRY_RUN_MEMORY"
                    or provenance.get("current_source_ref") != self._subject(source)
                    or provenance.get("same_version") is not False
                    or provenance.get("matched_report_source_ref") != {**self._subject(source),
                        "artifact_revision_id": uid("prior-draft:" + source.id),
                        "sha256": sha256(("prior-draft:" + source.id).encode()).hexdigest()}):
                return False
        return (query.stage == "query_verification" and query.state == "ACCEPTED" and isinstance(receipt, dict)
                and source.state == "PAUSED" and source.reason == "INTERNAL_QUESTION_PENDING"
                and receipt.get("transport") == "DRY_RUN_MEMORY" and receipt.get("query_task") == query.id
                and receipt.get("sha256") == self._receipt_hash(receipt)
                and receipt.get("source_task") == source.id and receipt.get("source_ref") == self._subject(source)
                and self._subject(verifier) == self._subject(source)
                and receipt.get("reading_world") == (verifier.reading_world or uid(f"world:{verifier.book}"))
                and receipt.get("result") in {"CONSISTENT", "CONTRADICTORY", "PENDING_EVALUATION", "STALE_REPORT"})

    def _continue_internal_question(self, verifier):
        source = self.jobs[verifier.source_task]
        query = self.jobs[verifier.dependencies[0]]
        receipt = query.query_receipt
        if receipt["result"] == "PENDING_EVALUATION":
            closure = self.add("world_coverage", source.book, source.chapter, source.revision,
                               dependencies=(verifier.id,), source_task=source.id)
            self.emit("WORLD_COVERAGE_ENROLLED", task=closure.id, source_ref=self._subject(source))
            return
        source.state, source.reason = "CANCELLED", "INTERNAL_QUESTION_RESOLVED"
        freeze = self.jobs[uid(f"{source.book}:{source.chapter}:freeze:0")]
        old_decision = self.jobs[freeze.dependencies[0]]
        old_decision.state, old_decision.reason = "CANCELLED", "SUPERSEDED_INTERNAL_QUESTION"
        resolution = ("CONFIRMED_CONFLICT" if receipt["result"] == "CONTRADICTORY" else
                      "STALE_REPORT" if receipt["result"] == "STALE_REPORT" else "NO_CONFLICT")
        self.emit("INTERNAL_QUESTION_RESOLVED", task=source.id, verification_task=verifier.id,
                  resolution=resolution, source_ref=self._subject(source), world_effects_applied=0)
        if resolution == "CONFIRMED_CONFLICT":
            strategy = self.add("strategy", source.book, source.chapter, source.revision + 1,
                                dependencies=(verifier.id,))
            freeze.dependencies = [strategy.id]
            self.emit("STRATEGY_REASSESSMENT_ENROLLED", task=strategy.id, source_task=source.id)
        else:
            dependencies = [verifier.id, *source.dependencies]
            if self.scenario in VERSION_SCENARIOS:
                draft = uid(f"{source.book}:{source.chapter}:draft:{source.revision}")
                knowledge = self.add("epistemic", source.book, source.chapter, source.revision,
                    dependencies=(draft, verifier.id), tag="version-reread")
                facts = self.add("facts", source.book, source.chapter, source.revision,
                    dependencies=(draft, knowledge.id), tag="version-reread")
                reconciled = self.add("reconcile", source.book, source.chapter, source.revision,
                    dependencies=(facts.id,), tag="version-reread")
                for reading in (knowledge, facts, reconciled):
                    reading.reading_world = verifier.reading_world
                dependencies = [verifier.id, draft, reconciled.id]
            review = self.add("review", source.book, source.chapter, source.revision,
                dependencies=dependencies, tag="internal-reread", source_task=source.id)
            review.reading_world = verifier.reading_world
            decision = self.add("chapter_decision", source.book, source.chapter, source.revision,
                dependencies=(review.id, *[key for key in dependencies if self.jobs[key].stage == "reconcile"]), tag="internal-reread")
            if self.scenario not in VERSION_SCENARIOS:
                decision.dependencies = [review.id, *[key for key in old_decision.dependencies if key != source.id]]
            freeze.dependencies = [decision.id]
            self.emit("SAME_DRAFT_REREAD_ENROLLED", task=review.id, source_task=source.id, source_ref=self._subject(source))

    def _return_chapter(self, job):
        freeze = self.jobs[uid(f"{job.book}:{job.chapter}:freeze:0")]
        old_decision = self.jobs[freeze.dependencies[0]]
        old_decision.state, old_decision.reason = "CANCELLED", "SUPERSEDED_REVIEW"
        maximum = (self.max_reworks + 1) * 8 - 1 if self.scenario == "novel" else self.max_reworks
        if job.revision >= maximum:
            freeze.state, freeze.reason = "FAILED", "REWORK_LIMIT_REACHED"
            self.emit("REWORK_LIMIT_REACHED", task=job.id)
            return
        revision = job.revision + 1
        if self.scenario == "novel" and revision % (self.max_reworks + 1) == 0:
            strategy = self.add("strategy", job.book, job.chapter, revision, dependencies=(job.id,))
            # Keep freezing blocked on a planner decision and the new reading.
            freeze.dependencies = [strategy.id]
            self.emit("STRATEGY_REASSESSMENT_ENROLLED", task=strategy.id)
        else:
            decision = self._chapter_candidate(job.book, job.chapter, revision, job.id, reading_world=job.reading_world)
            if job.reading_world:
                self.emit("SIMULATED_COVERAGE_LINEAGE_RENEWED", source_ref=self._subject(job),
                          successor_ref=self._subject(self.jobs[uid(f"{job.book}:{job.chapter}:draft:{revision}")]),
                          reading_world=job.reading_world, canonical_effects_applied=0)
            freeze.dependencies = [decision.id]
        self.emit("REWORK_ENROLLED", book=job.book, chapter=job.chapter, revision=revision)

    def _book_review(self, job):
        key = job.book, job.revision
        readers = [self.jobs[uid(f"{job.book}:0:{stage}:{job.revision}")] for stage in ("reader_1", "reader_2")]
        if key in self.handled_book_reviews or any(row.state != "ACCEPTED" for row in readers):
            return
        self.handled_book_reviews.add(key)
        if all(row.approved for row in readers):
            self.emit("INDEPENDENT_BOOK_REVIEWS_PASSED", book=job.book, edition=job.revision,
                      readers=[row.id for row in readers])
            return
        final = self.jobs[uid(f"{job.book}:0:final_editor:{job.revision}")]
        final.state, final.reason = "CANCELLED", "RETURNED_EDITION"
        release = self.jobs[uid(f"{job.book}:0:book_freeze:0")]
        maximum = (self.max_reworks + 1) * 8 - 1 if self.scenario == "novel" else self.max_reworks
        if job.revision >= maximum:
            release.state, release.reason = "FAILED", "BOOK_REVIEW_LIMIT_REACHED"
            self.emit("BOOK_REVIEW_LIMIT_REACHED", book=job.book)
            return
        revision = job.revision + 1
        writer = self.add("book_revision", job.book, revision=revision, dependencies=tuple(row.id for row in readers))
        facts = self.add("book_facts", job.book, revision=revision, dependencies=(writer.id,))
        reconciled = self.add("book_reconcile", job.book, revision=revision, dependencies=(facts.id,))
        editor = self._book_edition(job.book, revision, reconciled.id)
        release.dependencies = [editor.id]
        self.emit("BOOK_REVISION_ENROLLED", book=job.book, edition=revision)

    def _completed(self):
        for future in tuple(self.pending):
            if not future.done():
                continue
            identity, generation, attempt = self.pending.pop(future)
            job, call = self.jobs[identity], self.ledger[attempt]
            try:
                response = future.result()
                if response.raw_response.get("finish_reason") == "length":
                    raise ValueError("TRUNCATED")
                self._deliver(job, generation, attempt, response)
            except ModelCallFailure as error:
                if error.sent:
                    job.state, job.reason = "PAUSED", "CALL_RECONCILIATION_REQUIRED"
                    call["state"] = "UNKNOWN"
                    self.emit("UNKNOWN_HELD", task=job.id, attempt=attempt)
                    if self.scenario == "unknown_reconciled":
                        def reconcile(job=job, call=call):
                            call["state"], call["reserved"] = "FAILED", 0
                            job.state, job.reason, job.ready_at = "READY", None, self.now
                            self.emit("SIMULATED_PROVIDER_NOT_EXECUTED_PROOF", task=job.id)
                            self._wake()
                        self.schedule(3, reconcile)
                else:
                    call["state"], call["reserved"] = "FAILED", 0
                    if error.code == "MODEL_CALL_NOT_AUTHORIZED":
                        job.state, job.reason = "PAUSED", "AUTHORIZATION_REQUIRED"
                    else:
                        self._retry(job, error.code)
            except (ValueError, KeyError):
                call["state"], call["reserved"] = "FAILED", 0
                self._retry(job, "INVALID_MODEL_OUTPUT")
            except BrokenExecutor:
                call["state"], call["reserved"] = "FAILED", 0
                self._retry(job, "WORKER_POOL_BROKEN_BEFORE_SEND")

    def _restart(self):
        # Restart between admitted batches. Sent-call death is covered separately
        # by crash_after_send/UNKNOWN, which cannot silently become READY.
        snapshot = json.loads(json.dumps({"jobs": [asdict(job) for job in self.jobs.values()],
            "ledger": self.ledger, "acceptances": self.acceptances, "frozen": list(self.frozen),
            "chapter_manifests": list(self.chapter_manifests.values()), "releases": self.releases}))
        self.jobs = {row["id"]: Job(**row) for row in snapshot["jobs"]}
        self.ledger, self.acceptances = snapshot["ledger"], snapshot["acceptances"]
        self.frozen = {tuple(value) for value in snapshot["frozen"]}
        self.chapter_manifests = {(row["book"], row["chapter"]): row for row in snapshot["chapter_manifests"]}
        self.releases = {int(key): value for key, value in snapshot["releases"].items()}
        self.lifecycle.shutdown()
        self.lifecycle = self.new_lifecycle()
        self.used_faults.add("restart")
        self.emit("PROGRAM_RESTARTED_FROM_MEMORY_CHECKPOINT")

    def run(self):
        try:
            while self.steps < self.max_steps:
                self.steps += 1
                while self.events and self.events[0][0] <= self.now:
                    _, _, action = heapq.heappop(self.events)
                    action()
                self._completed()
                self.lifecycle.maintain()
                if self.scenario == "restart" and not self.pending and self.acceptances and "restart" not in self.used_faults:
                    self._restart()
                if self.notified or self.now >= self.next_scan:
                    self.emit("DURABLE_SCAN", source="notification" if self.notified else "periodic")
                    self.notified, self.next_scan = False, self.now + self.poll_seconds
                    if not self.storage_available:
                        self.emit("STORAGE_UNAVAILABLE_BACKOFF")
                    else:
                        self._ready()
                        for job in list(self.jobs.values()):
                            if len(self.pending) >= self.workers:
                                break
                            if job.state == "READY" and (not self.coverage_waiting or job.stage in PROGRAM_STAGES):
                                self._dispatch(job)
                self._check()
                if not self.pending and not self.events and not any(job.state == "READY" for job in self.jobs.values()):
                    self._ready()
                    if not any(job.state == "READY" for job in self.jobs.values()):
                        self.now += self.lifecycle.idle_seconds
                        self.lifecycle.maintain()
                        break
                if self.notified:
                    continue
                deadlines = [self.next_scan]
                if self.events:
                    deadlines.append(self.events[0][0])
                idle = self.lifecycle.idle_remaining()
                if idle is not None:
                    deadlines.append(self.now + idle)
                self.now = max(self.now + 0.001, min(deadlines))
            else:
                self.problems.append("STEP_LIMIT_REACHED")
            self._check(final=True)
            return self.report()
        finally:
            self.lifecycle.shutdown()

    def _check(self, final=False):
        if self.max_inflight > self.workers:
            self.problems.append("CONCURRENCY_LIMIT_EXCEEDED")
        if any(count != 1 for count in self.acceptances.values()):
            self.problems.append("DUPLICATE_ACCEPTANCE")
        attempts = [row["attempt"] for row in self.trace if row["event"] == "HARNESS_REQUEST"]
        if len(attempts) != len(set(attempts)):
            self.problems.append("DUPLICATE_HARNESS_DISPATCH")
        for job in self.jobs.values():
            if job.state == "ACCEPTED" and job.dependencies:
                accepted = [self.jobs[key].state == "ACCEPTED" for key in job.dependencies]
                if not (all(accepted) if job.dependency_mode == "AND" else any(accepted)):
                    self.problems.append("DEPENDENCY_GATE_BYPASSED")
        if any(job.stage == "freeze" and job.state == "ACCEPTED"
               and any(self.jobs[key].state != "ACCEPTED" for key in job.dependencies) for job in self.jobs.values()):
            self.problems.append("FREEZE_BEFORE_REVIEW")
        for call in self.ledger.values():
            if call["state"] == "UNKNOWN" and (call["reserved"] != 1 or self.jobs[call["task"]].state != "PAUSED"):
                self.problems.append("UNKNOWN_RESERVATION_LOST_OR_RETRIED")
        if final:
            if any(job.state == "RUNNING" for job in self.jobs.values()):
                self.problems.append("ORPHAN_RUNNING_TASK")
            if (any(job.state == "BLOCKED" for job in self.jobs.values())
                    and not any(job.state in {"PAUSED", "FAILED"} for job in self.jobs.values())
                    and not self.events):
                self.problems.append("UNEXPECTED_DEADLOCK")
            if self.scenario in {"late_result", "lease_expired"} and self.late_results != 1:
                self.problems.append("LATE_RESULT_FENCE_NOT_EXERCISED")
            if self.scenario == "restart" and "restart" not in self.used_faults:
                self.problems.append("RESTART_NOT_EXERCISED")
            if self.scenario in INTERNAL_SCENARIOS and "internal_question" not in self.used_faults:
                self.problems.append("INTERNAL_QUERY_PATH_NOT_EXERCISED")
            if self.scenario in MCP_SCENARIOS and not any(row["event"] == "REVIEW_MCP_EVIDENCE_MISSING" for row in self.trace):
                self.problems.append("MCP_RECOVERY_PATH_NOT_EXERCISED")
            expected_coverage = (sum(row["event"] == "WORLD_COVERAGE_ENROLLED" for row in self.trace)
                                 if self.scenario == "multi_chapter_coverage" else 1)
            if (self.scenario in COVERAGE_SCENARIOS - {"coverage_semantic_changed"}
                    and sum(row["event"] == "SIMULATED_FORMAL_COVERAGE_ACCEPTED" for row in self.trace) != expected_coverage):
                self.problems.append("FORMAL_COVERAGE_PATH_NOT_EXERCISED")
            if self.scenario == "coverage_semantic_changed" and not any(row["event"] == "SEMANTIC_WORLD_CHANGE_REJECTED" for row in self.trace):
                self.problems.append("SEMANTIC_CHANGE_REFUSAL_NOT_EXERCISED")

    def report(self):
        complete = (all(job.state in TERMINAL for job in self.jobs.values())
                    and not any(job.state == "FAILED" for job in self.jobs.values())
                    and (not self.jobs or len(self.releases) == self.books))
        return {"scenario": self.scenario, "seed": self.seed, "execution_kind": "DRY_RUN",
                "status": "PASS" if not self.problems else "FAIL",
                "workflow_status": "COMPLETED" if complete else "STOPPED_WITH_BLOCKERS",
                "virtual_seconds": round(self.now, 3), "steps": self.steps,
                "logical_agents": self.logical_agents, "logical_agent_source": "SYNTHETIC_REGISTRY_SIZE",
                "storage_backend": "IN_MEMORY", "budget_unit": "SIMULATED_CALL_RESERVATION",
                "harness_path": "RuntimeModelAdapter.generate -> providers.invoke -> dry_run.generate",
                "harness_requests": sum(row["event"] == "HARNESS_REQUEST" for row in self.trace),
                "upstream_model_calls": 0, "database_connections": 0,
                "max_inflight": self.max_inflight, "worker_limit": self.workers,
                "worker_pool_events": self.worker_events, "idle_worker_pools": int(self.lifecycle.state != "DORMANT"),
                "frozen_chapters": len(self.frozen), "requested_chapters": self.books * self.chapters if self.jobs else 0,
                "goal": "BOOK_RELEASE_FREEZE", "frozen_books": len(self.releases),
                "requested_books": self.books if self.jobs and self.scenario != "dependencies" else 0,
                "releases": [self.releases[book] for book in sorted(self.releases)],
                "chapter_manifests": [self.chapter_manifests[key] for key in sorted(self.chapter_manifests)],
                "late_results_rejected": self.late_results, "duplicate_results_ignored": self.duplicate_results,
                "unknown_reservations_held": sum(c["reserved"] for c in self.ledger.values() if c["state"] == "UNKNOWN"),
                "max_ready_to_claim_seconds": max(self.wake_delays, default=0),
                "states": {state: sum(job.state == state for job in self.jobs.values())
                           for state in sorted({job.state for job in self.jobs.values()})},
                "blockers": [{"task": job.id, "reason": job.reason} for job in self.jobs.values() if job.reason],
                "invariant_failures": sorted(set(self.problems)),
                "stage_counts": {stage: sum(job.stage == stage for job in self.jobs.values())
                                 for stage in sorted({job.stage for job in self.jobs.values()})}, "trace": self.trace}
