"""P2-03 PostgreSQL gates with explicitly labelled deterministic model fixtures."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Barrier, Event
from uuid import uuid4

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import ModelCallFailure, ModelResponse, OpenAICompatibleAdapter, TrustedHostAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class FixtureAdapter:
    """No model is called; evidence explicitly says TEST_FIXTURE."""
    def __init__(self, hook=None, *, amount=Decimal("0.01"), malformed_actor=False):
        self.calls, self.hook, self.amount, self.malformed_actor = [], hook, amount, malformed_actor

    @property
    def configuration(self):
        return {"adapter_kind": "DETERMINISTIC_TEST", "provider": "fixture", "model": "p2-gateway-fixture-v1",
                "max_output_tokens": 1024, "timeout_seconds": 10, "isolation": "STATELESS_TEXT_ONLY",
                "isolation_evidence": "Unit-controlled fixture", "execution_kind": "TEST_FIXTURE"}

    def check_available(self):
        pass

    def generate(self, request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        packet = json.loads(request.messages[1]["content"])
        context = packet["context"]
        payload = {"text": "他在桥前停步，重新看了一遍绳结。", "event_refs": [],
                   "important_deltas": [], "evidence_refs": []}
        if context["role_id"] == "character_actor":
            payload = {"schema_version": "2.0", "proposal_id": str(uuid4()), "task_id": context["task_id"],
                       "generation": context["generation"], "context_bundle_id": context["context_bundle_id"],
                       "world_ref": context["world_ref"], "actor_id": context["actor_id"], "intent": "询问此地是否安全",
                       "targets": [], "story_interval": {"start": point(1), "end": point(2), "bounds": "[)"},
                       "resource_requests": [], "preconditions": [], "evidence_refs": [],
                       "dependency_fingerprint": context["dependency_fingerprint"], "idempotency_key": "fixture-action"}
        envelope = {"schema_version": "2.1", **{k: context[k] for k in ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")},
                    "status": "SUCCEEDED", "payload": payload, "unresolved_questions": []}
        if self.malformed_actor:
            # A valid outer envelope must not smuggle a different actor in the
            # selected ActionProposal's nested identity fields.
            envelope["payload"]["actor_id"] = str(uuid4())
        return ModelResponse(text=json.dumps(envelope, ensure_ascii=False), provider_receipt="fixture:" + request.request_sha256,
            input_tokens=25, output_tokens=40, cached_input_tokens=0,
            actual_amount=self.amount, currency="USD" if self.amount is not None else None,
            raw_response={"fixture": True}, request_sha256=request.request_sha256)


def setup_gateway(env, adapter=None, *, role="writer", materials=(), private_material=False,
                  world=None, actor_key="protagonist", story_time=None, task_inputs=()):
    kernel = env["kernel"]
    story_time = point(1) if story_time is None else story_time
    world = load_fixture(kernel) if world is None else world
    kind = "character_action" if role == "character_actor" else "scene_draft"
    adapter = adapter or FixtureAdapter()
    gateway = AgentGateway(kernel, {"fixture": adapter})
    if private_material:
        materials = [kernel.put_artifact(world["principal"], world["world_ref"]["work_id"],
                                        {"secret": "future-only-cause"}, "TEXT")]
    prepared = gateway.prepare_input(world["principal"], world["world_ref"], role_id=role, task_kind=kind,
        assignment="完成当前合法上下文允许的候选任务；未知内容保持未知。", adapter_id="fixture", material_refs=materials)
    actor_id = world["ids"][actor_key] if role == "character_actor" else None
    plane = "BELIEF" if actor_id else "WORLD"
    granted = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"], role_id=role, actor_id=actor_id,
        branch_ids=[world["world_ref"]["branch_id"]], capabilities=["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM"],
        planes=[plane], max_story_time=story_time)
    principal = granted["principal"]
    manifest = kernel.put_artifact(world["principal"], world["world_ref"]["work_id"],
        {"subject_ids": [world["ids"]["sword"]], "predicates": ["owner", "active_holder"]}, "INPUT_MANIFEST")
    spec = {"schema_version": "2.0", "task_id": str(uuid4()), "generation": 0, "role_id": role, "task_kind": kind,
            "state": "READY", "world_ref": world["world_ref"], "story_time": story_time, "context_bundle_id": None,
            "policy_version": "nf-policy-1", "dependency_fingerprint": None,
            "runtime_contract_version": prepared["runtime_contract_version"], "capability_grant_id": principal.grant_id,
            "input_artifacts": [manifest, prepared["input_ref"], *task_inputs],
            "budget": {"currency": "USD", "max_amount": "1", "token_budget": 250000},
            "wall_deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    kernel.create_task(principal, spec, spec["task_id"])
    worker = "gateway-worker:" + spec["task_id"]
    task = kernel.claim_task(principal, worker, task_id=spec["task_id"], lease_seconds=600)
    cut = {"plane": plane}
    if actor_id:
        cut.update(actor_id=actor_id, known_at=story_time)
    compiled = WorldQueryService(kernel).context_compile(principal, {
        "schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "recipe_id": "ACTOR" if actor_id else "WRITER", "input_manifest_ref": manifest,
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": world["world_ref"]},
                          "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": story_time},
        "token_budget": 20000, "idempotency_key": "compile:" + task["task_id"]})
    assert compiled["status"] == "READY"
    budget = kernel.tasks.create_budget(world["world_ref"]["work_id"], "2")
    request = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
               "worker_id": worker, "input_ref": prepared["input_ref"], "budget_id": budget,
               "call_key": "first", "reserve_amount": "0.2", "token_reservation": 100000}
    return {"kernel": kernel, "world": world, "gateway": gateway, "principal": principal,
            "request": request, "adapter": adapter, "task": task, "spec": spec}


def test_validated_candidate_replays_without_model_or_world_effects(kernel_env):
    item = setup_gateway(kernel_env)
    kernel, request = item["kernel"], item["request"]
    result = item["gateway"].execute(item["principal"], request)
    assert result["validation_status"] == "VALIDATED_CANDIDATE" and result["execution_kind"] == "TEST_FIXTURE"
    assert item["gateway"].execute(item["principal"], request) == result
    assert len(item["adapter"].calls) == 1
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, item["world"]["world_ref"]["work_id"]) == item["world"]["world_ref"]
        assert repo.many("events", {"work_id": item["world"]["world_ref"]["work_id"]}) == []
        call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        assert call["state"] == "SUCCEEDED" and call["actual_tokens"] == 65
        budget = repo.one("budgets", {"budget_id": request["budget_id"]})
        assert budget["spent_amount"] == Decimal("0.01") and budget["reserved_amount"] == 0
    changed = {**request, "reserve_amount": "0.3"}
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], changed)
    assert error.value.code == "IDEMPOTENCY_MISMATCH"


def test_unknown_actual_cost_preserves_money_and_measured_usage(kernel_env):
    item = setup_gateway(kernel_env, FixtureAdapter(amount=None))
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["status"] == "RESULT_STORED" and result["cost_status"] == "UNKNOWN_COST"
    with item["kernel"].db.uow(write=False) as repo:
        call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        assert call["state"] == "UNKNOWN" and call["actual_amount"] is None
        budget = repo.one("budgets", {"budget_id": item["request"]["budget_id"]})
        assert budget["reserved_amount"] == Decimal("0.2") and budget["spent_amount"] == 0
        evidence = item["kernel"].artifact(repo, result["evidence_ref"], item["world"]["world_ref"]["work_id"])["payload"]
        assert evidence["usage"]["input_tokens"] == 25 and evidence["usage"]["output_tokens"] == 40
        assert evidence["usage"]["actual_amount"] is None


def test_timeout_retains_unknown_and_cannot_retry_using_another_key(kernel_env):
    def timeout(_):
        raise ModelCallFailure("MODEL_TRANSPORT_UNKNOWN")
    item = setup_gateway(kernel_env, FixtureAdapter(timeout))
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["status"] == "BLOCKED" and result["cost_status"] == "UNKNOWN_COST"
    assert item["gateway"].execute(item["principal"], item["request"])["status"] == "BLOCKED"
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], {**item["request"], "call_key": "blind-retry"})
    assert error.value.code == "CALL_OUTCOME_UNKNOWN" and len(item["adapter"].calls) == 1


def test_nested_wrong_actor_is_not_an_accepted_result(kernel_env):
    item = setup_gateway(kernel_env, FixtureAdapter(malformed_actor=True), role="character_actor")
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["validation_status"] == "REJECTED" and result["output_ref"] is None
    assert item["kernel"].get_task(item["principal"], item["task"]["task_id"])["state"] == "FAILED"
    with item["kernel"].db.uow(write=False) as repo:
        audit = item["kernel"].artifact(repo, result["evidence_ref"], item["world"]["world_ref"]["work_id"])["payload"]
        assert audit["reason"] == "MODEL_IDENTITY_MISMATCH" and audit["response_ref"] is not None


@pytest.mark.parametrize("action", ["cancel", "revoke"])
def test_cancel_or_revoke_during_model_wait_retains_evidence_but_blocks_delivery(kernel_env, action):
    item = setup_gateway(kernel_env)
    def act(_):
        assert item["kernel"].db.engine.pool.checkedout() == 0
        if action == "cancel":
            item["kernel"].cancel_task(item["principal"], item["task"]["task_id"], item["task"]["generation"], "stop-during-call")
        else:
            item["kernel"].revoke_grant(item["world"]["principal"], item["principal"].grant_id)
    item["adapter"].hook = act
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], item["request"])
    assert error.value.code in {"VIEW_DENIED", "STALE_GENERATION", "TASK_NOT_ACCEPTABLE"}
    with item["kernel"].db.uow(write=False) as repo:
        evidence = repo.many("artifacts", {"work_id": item["world"]["world_ref"]["work_id"], "kind": "AGENT_CALL_EVIDENCE"})
        assert len(evidence) == 1
        assert repo.many("task_results", {"task_id": item["task"]["task_id"]}) == []
        assert kernel_env["kernel"].head(repo, item["world"]["world_ref"]["work_id"]) == item["world"]["world_ref"]


def test_missing_credential_is_blocked_before_any_call_or_reservation(kernel_env):
    adapter = OpenAICompatibleAdapter(endpoint="https://example.test/v1/chat/completions", model="explicit-model", api_key=None)
    item = setup_gateway(kernel_env, adapter)
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["status"] == "BLOCKED" and result["reason"] == "MODEL_UNAVAILABLE" and result["cost_status"] == "NOT_SENT"
    with item["kernel"].db.uow(write=False) as repo:
        assert repo.many("call_attempts", {"task_id": item["task"]["task_id"]}) == []
        assert repo.one("budgets", {"budget_id": item["request"]["budget_id"]})["reserved_amount"] == 0


def test_character_host_without_verified_isolation_cannot_dispatch(kernel_env):
    host = TrustedHostAdapter(lambda _: pytest.fail("Host must not be called"), host="fixture", model="fixture")
    item = setup_gateway(kernel_env, host, role="character_actor")
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], item["request"])
    assert error.value.code == "HOST_ISOLATION_UNVERIFIED"


@pytest.mark.concurrency
def test_same_call_concurrently_dispatches_only_once(kernel_env):
    entered, release = Event(), Event()
    def hold(_):
        entered.set()
        assert release.wait(timeout=10)
    item = setup_gateway(kernel_env, FixtureAdapter(hold))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(item["gateway"].execute, item["principal"], item["request"])
        assert entered.wait(timeout=10)
        second = pool.submit(item["gateway"].execute, item["principal"], item["request"])
        other = second.result(timeout=10)
        assert other["status"] == "BLOCKED"
        release.set()
        assert first.result(timeout=10)["status"] == "RESULT_STORED"
    assert len(item["adapter"].calls) == 1


@pytest.mark.concurrency
def test_distinct_call_keys_race_is_fenced_inside_reservation_transaction(kernel_env, monkeypatch):
    # Force both calls past the optimistic pending read before either reserve.
    # The authoritative task-row check must reject one, not rely on that read.
    at_reserve, entered, release = Barrier(2), Event(), Event()
    def hold(_):
        entered.set()
        assert release.wait(timeout=10)
    item = setup_gateway(kernel_env, FixtureAdapter(hold))
    original = item["kernel"].tasks.reserve_call
    def simultaneous(*args, **kwargs):
        at_reserve.wait(timeout=10)
        return original(*args, **kwargs)
    monkeypatch.setattr(item["kernel"].tasks, "reserve_call", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(item["gateway"].execute, item["principal"], {**item["request"], "call_key": key})
                   for key in ("racing-one", "racing-two")]
        try:
            assert entered.wait(timeout=10)
            completed, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            assert len(completed) == 1 and len(pending) == 1
            error = next(iter(completed)).exception()
            assert isinstance(error, DomainError) and error.code == "CALL_OUTCOME_UNKNOWN"
        finally:
            release.set()
        assert next(iter(pending)).result(timeout=10)["status"] == "RESULT_STORED"
    assert len(item["adapter"].calls) == 1


def test_unbound_input_wrong_generation_or_worker_never_calls_model(kernel_env):
    item = setup_gateway(kernel_env)
    for patch, code in [({"generation": 2}, "STALE_GENERATION"), ({"worker_id": "other"}, "LEASE_OWNER_MISMATCH"),
                        ({"input_ref": {**item["request"]["input_ref"], "artifact_revision_id": str(uuid4())}}, "VIEW_DENIED")]:
        with pytest.raises(DomainError) as error:
            item["gateway"].execute(item["principal"], {**item["request"], **patch})
        assert error.value.code == code
    assert item["adapter"].calls == []


def test_input_manifest_does_not_grant_access_to_private_material(kernel_env):
    item = setup_gateway(kernel_env, role="character_actor", private_material=True)
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], item["request"])
    assert error.value.code == "VIEW_DENIED" and item["adapter"].calls == []


def test_actor_receives_only_own_compiled_belief_and_frozen_runtime(kernel_env):
    item = setup_gateway(kernel_env, role="character_actor")
    # Changing a deployment catalog cannot rewrite already prepared task bytes.
    def changed_catalog(*args, **kwargs):
        raise AssertionError("Running task must not load a new runtime")
    item["gateway"].catalog.snapshot = changed_catalog
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    request = item["adapter"].calls[0]
    packet = json.loads(request.messages[1]["content"])
    assert packet["read_cut"]["knowledge_cut"]["plane"] == "BELIEF"
    assert packet["context"]["actor_id"] == item["world"]["ids"]["protagonist"]
    assert item["world"]["ids"]["smith"] not in json.dumps(packet["context"]["facts"])


def test_protocol_failure_keeps_raw_output_and_never_creates_a_candidate(kernel_env):
    class InvalidOutput(FixtureAdapter):
        def generate(self, request):
            self.calls.append(request)
            return ModelResponse(text="not a JSON model envelope", provider_receipt="invalid-fixture",
                input_tokens=20, output_tokens=3, actual_amount=Decimal("0.01"), currency="USD",
                request_sha256=request.request_sha256)
    item = setup_gateway(kernel_env, InvalidOutput())
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["validation_status"] == "REJECTED" and result["output_ref"] is None
    with item["kernel"].db.uow(write=False) as repo:
        evidence = item["kernel"].artifact(repo, result["evidence_ref"], item["world"]["world_ref"]["work_id"])["payload"]
        assert evidence["reason"] == "INVALID_MODEL_OUTPUT"
        response = item["kernel"].artifact(repo, evidence["response_ref"], item["world"]["world_ref"]["work_id"])["payload"]
        assert response["text"] == "not a JSON model envelope"
