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
             "storage_disconnect", "worker_pool_failure", "context_mismatch", "book_review_return")
TERMINAL = {"ACCEPTED", "FAILED", "CANCELLED"}
PROGRAM_STAGES = {"marker", "reconcile", "freeze", "continuity", "assemble", "book_reconcile", "book_freeze"}
ROLE_STAGES = {
    "plan": "world_designer", "volume_plan": "volume_director", "scene_plan": "volume_director",
    "roleplay": "character_actor", "adjudicate": "adjudication_advisor", "draft": "writer",
    "epistemic": "reviewer", "facts": "evidence_maintainer", "review": "reviewer",
    "chapter_decision": "chief_editor", "strategy": "chief_editor", "memory": "evidence_maintainer",
    "book_structure": "literary_editor", "book_seam": "literary_editor", "book_cross_volume": "literary_editor",
    "book_revision": "writer", "book_facts": "evidence_maintainer",
    "reader_1": "reviewer", "reader_2": "reviewer", "final_editor": "chief_editor",
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
        self.late_results = self.duplicate_results = self.max_inflight = 0
        self.wake_delays, self.problems = [], []
        self.next_scan, self.poll_seconds, self.notified = 0.0, 5.0, False
        self.worker_events = []
        self.storage_available = True
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

    def add(self, stage, book, chapter=0, revision=0, dependencies=(), state=None):
        identity = uid(f"{book}:{chapter}:{stage}:{revision}")
        job = Job(identity, stage, book, chapter, revision, list(dependencies),
                  state=state or ("BLOCKED" if dependencies else "READY"), ready_at=self.now)
        self.jobs[identity] = job
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
            assembled = self.add("assemble", book, dependencies=(*chapter_freezes, previous.id))
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

    def _chapter_candidate(self, book, chapter, revision, predecessor):
        draft = self.add("draft", book, chapter, revision, dependencies=(predecessor,))
        knowledge = self.add("epistemic", book, chapter, revision, dependencies=(draft.id,))
        facts = self.add("facts", book, chapter, revision, dependencies=(draft.id, knowledge.id))
        reconciled = self.add("reconcile", book, chapter, revision, dependencies=(facts.id,))
        review = self.add("review", book, chapter, revision, dependencies=(draft.id, reconciled.id))
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
        if (fault_stage and fault not in self.used_faults
                and fault in {"rate_limit", "timeout", "crash_before_send", "crash_after_send", "invalid_json", "truncated", "unknown_reconciled", "context_mismatch"}):
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
                                  "world_revision_id": uid(f"world:{job.book}")}}
        messages = role_messages("DRY_RUN: isolated role, simulated JSON output only.",
            {"assignment": "Simulate the assigned stage", "context": identity,
             "dry_run_subject_ref": self._subject(job),
             "knowledge_cut": {"plane": "BELIEF" if role == "character_actor" else "WORLD"}}, identity)
        rate = 1 if job.stage == "epistemic" else self.pass_rate
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
                  logical_agent_id=uid(f"agent:{job.book}:{job.stage if job.stage in {'reader_1','reader_2'} else role}"))
        # The normal production adapter forwards dry_run to the normal provider
        # dispatcher. Only its final execution backend is substituted.
        adapter = RuntimeModelAdapter(profile, MemoryJournal(self, job.id, generation, attempt))
        adapter.check_available(dry_run=True)
        response = adapter.generate(request)
        if not response.dry_run:
            raise AssertionError("Dry-run Harness returned a live response")
        return response

    def _dispatch(self, job):
        if job.stage in PROGRAM_STAGES:
            if job.stage == "freeze":
                decision = self.jobs[job.dependencies[0]]
                reviews = [self.jobs[key] for key in decision.dependencies if self.jobs[key].stage == "review"]
                if decision.state != "ACCEPTED" or len(reviews) != 1 or reviews[0].approved is not True:
                    job.state, job.reason = "FAILED", "CHAPTER_FREEZE_GATE_REJECTED"
                    self.problems.append(job.reason)
                    return
            if job.stage == "book_freeze":
                editor = self.jobs[job.dependencies[0]]
                readers = [self.jobs[key] for key in editor.dependencies]
                if (editor.state != "ACCEPTED" or editor.approved is not True or len(readers) != 2
                        or {row.stage for row in readers} != {"reader_1", "reader_2"}
                        or any(row.state != "ACCEPTED" or row.approved is not True for row in readers)
                        or len({self._subject(row)["sha256"] for row in readers}) != 1
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
                    "draft_task": uid(f"{job.book}:{job.chapter}:draft:{revision}"),
                    "facts_task": uid(f"{job.book}:{job.chapter}:facts:{revision}"),
                    "review_task": reviews[0].id, "editor_task": decision.id}
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

    def _accept(self, job):
        if job.state == "ACCEPTED":
            self.duplicate_results += 1
            return
        job.state, job.reason = "ACCEPTED", None
        self.acceptances[job.id] = self.acceptances.get(job.id, 0) + 1
        self.emit("TASK_ACCEPTED", task=job.id, generation=job.generation)
        self._wake()

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
                  approved=response.raw_response["review_approved"])
        role = ROLE_STAGES[job.stage]
        job.approved = (not blocking_review_issues(envelope["payload"]["issues"]) if role == "reviewer" else
                        envelope["payload"].get("decision") == "DRY_RUN_APPROVE" if role == "chief_editor" else None)
        self._accept(job)
        if job.stage == "review" and not job.approved:
            self._return_chapter(job)
        elif job.stage == "strategy":
            decision = self._chapter_candidate(job.book, job.chapter, job.revision, job.id)
            self.jobs[uid(f"{job.book}:{job.chapter}:freeze:0")].dependencies = [decision.id]
        elif job.stage in {"reader_1", "reader_2"}:
            self._book_review(job)
        if self.scenario == "duplicate_result":
            self._accept(job)

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
            decision = self._chapter_candidate(job.book, job.chapter, revision, job.id)
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
                            if job.state == "READY":
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
