"""Real NF grants, W3 delegation, task budgets and PostgreSQL pointer contention.

The W4 response here is a transport fixture. The two-service suite exercises
the same NF authority against live Workshop employment, sharing and freeze.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
import test_workshop_delegation

from novel_factory.application import workshop_skills
from novel_factory.application.delegation import revoke
from novel_factory.application.dynamic_agents import DynamicAgentService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

delegated_site = test_workshop_delegation.site

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def setup(delegated_site, monkeypatch):
    delegation, submit, _, _, _, owner, requests, definition = delegated_site
    k, wr = delegation.kernel, definition.spec["world_ref"]
    bound = delegation.call("nf_delegation_submit", submit)
    source = bound["task_id"], bound["generation"]
    with k.db.uow(write=False) as repo:
        install = repo.one("delegation_installations", {"installation_id": delegation.installation_id})
        slot = repo.one(
            "delegation_slots",
            {"installation_id": delegation.installation_id, "scope_ref": submit["scope_ref"]},
        )
        task = repo.one("tasks", {"task_id": source[0]})
        budget = slot["job_config"]["dispatch"]["budget_id"]
    employee = str(uuid4())
    approved = {}
    for i, text in enumerate(["登记阴影中的细节。", "登记不愿开口的人。", "登记正在改变的物件。"], 1):
        binding = {
            "org_id": install["org_id"],
            "installation_id": install["installation_id"],
            "employee_id": employee,
            "employment_revision": 1,
            "grant_epoch": install["epoch"],
            "binding_id": str(uuid4()),
            "skill_id": "precise-reading",
            "source_revision": "v" + str(i),
            "content_sha": sha256(text.encode()).hexdigest(),
        }
        approved[binding["binding_id"]] = (binding, text)
    expires = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()

    async def responder(binding_id, nf, task, freeze, employee):
        binding = approved[binding_id][0]
        return {
            "nf": nf,
            "task": task,
            "frozen": freeze,
            "binding": binding,
            "skill_mode": submit["task_template_id"],
            "skill_epoch": 1,
            "expires_at": expires,
        }

    monkeypatch.setattr(workshop_skills, "_call", responder)
    service = DynamicAgentService(k, config=CreativeHarnessConfig(enabled=True, dynamic_agents=True))
    base = {
        "requested_tools": ["context_get"],
        "trigger": "TASK_DEMAND",
        "evidence_refs": [definition.manifest_ref],
        "quota": {
            "budget_id": budget,
            "currency": "USD",
            "max_amount": "0.05",
            "max_tokens": 1000,
            "max_attempts": 2,
            "deadline": (min(task["deadline"], datetime.now(UTC) + timedelta(minutes=10))).isoformat(),
        },
    }

    def arguments(index):
        binding, text = list(approved.values())[index]
        return {
            **base,
            "workshop_binding": binding,
            "skill_instructions": text,
            "profile_version": "p" + str(index + 1),
            "role_instructions": "按本次角色授权收集正文材料。" + str(index + 1),
            "idempotency_key": "register" + str(index),
        }

    return k, wr, owner, source, service, arguments, requests


def test_scoped_versions_rollback_frozen_bytes_and_archival(setup):
    k, wr, owner, source, svc, args, requests = setup
    first = svc.register_revision(owner, *source, **args(0))
    second = svc.register_revision(owner, *source, **args(1))
    instance = first["instance_ref"]
    assert first == svc.register_revision(owner, *source, **args(0))
    assert instance == second["instance_ref"]
    assert svc.history(owner, wr, instance_ref=instance)["state"] == "REGISTERED"
    select = lambda ref, epoch, key: svc.select(
        owner, wr, instance_ref=instance, revision_ref=ref, expected_epoch=epoch, idempotency_key=key
    )
    selected = select(first["revision_ref"], 0, "one")
    assert selected == select(first["revision_ref"], 0, "one")

    def freeze(task_id):
        return svc.freeze(
            owner,
            wr,
            role_id="evidence_maintainer",
            task_kind="summary",
            task_id=task_id,
            instance_ref=instance,
        )

    tid = str(uuid4())
    old = freeze(tid)
    assert select(second["revision_ref"], 1, "two")["epoch"] == 2
    fresh = freeze(str(uuid4()))
    assert old == freeze(tid) and fresh != old
    with k.db.uow(write=False) as repo:
        payload = k.artifact(repo, fresh, wr["work_id"])["payload"]
        assert payload["skill_instructions"] == args(1)["skill_instructions"]
        assert payload["skill_sha256"] == args(1)["workshop_binding"]["content_sha"]
        assert (
            k.artifact(repo, old, wr["work_id"])["payload"]["profile_version"] == args(0)["profile_version"]
        )
        proposal = validate_contract(
            "RoleSkillProposal", k.artifact(repo, first["proposal_ref"], wr["work_id"])["payload"], "harness"
        )
        assert (
            proposal["scope"]["task_id"] == source[0]
            and proposal["parent_runtime"] == proposal["fallback_runtime"]
        )
        assert proposal["evidence_refs"] == args(0)["evidence_refs"]
    back = select(first["revision_ref"], 2, "rollback")
    assert back["epoch"] == 3
    closed_args = {"instance_ref": instance, "action": "EXIT", "expected_epoch": 3, "idempotency_key": "exit"}
    closed = svc.retire(owner, wr, **closed_args)
    assert closed == svc.retire(owner, wr, **closed_args) and closed["state"] == "EXITED"
    with pytest.raises(DomainError):
        freeze(tid)
    with pytest.raises(DomainError):
        select(second["revision_ref"], 4, "resurrect")
    svc.retire(
        owner, wr, instance_ref=instance, action="ARCHIVE", expected_epoch=4, idempotency_key="archive"
    )
    log = svc.history(owner, wr, instance_ref=instance)
    assert [e["state"] for e in log["events"]] == ["ACTIVE", "ACTIVE", "ACTIVE", "EXITED", "ARCHIVED"]
    assert [v["revision_ref"] for v in log["revisions"]] == [first["revision_ref"], second["revision_ref"]]
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wr["work_id"], wr["branch_id"]) == wr
        assert not requests and len(repo.many("tasks", {"work_id": wr["work_id"]})) == 1


def test_competing_pointer_selections_are_atomic(setup):
    _, wr, owner, source, svc, args, _ = setup
    versions = [svc.register_revision(owner, *source, **args(i)) for i in (0, 1)]
    barrier = Barrier(2)

    def attempt(index):
        barrier.wait(timeout=4)
        item = versions[index]
        try:
            return svc.select(
                owner,
                wr,
                instance_ref=item["instance_ref"],
                revision_ref=item["revision_ref"],
                expected_epoch=0,
                idempotency_key="select-" + str(index),
            )
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert [r for r in outcomes if isinstance(r, str)] == ["DYNAMIC_SELECTION_CONFLICT"]
    (active,) = [r for r in outcomes if isinstance(r, dict)]
    history = svc.history(owner, wr, instance_ref=active["instance_ref"])
    assert history["revision_ref"] == active["revision_ref"] and len(history["events"]) == 1


def test_off_foreign_scope_and_permission_increase_are_rejected(setup):
    k, wr, owner, source, svc, args, _ = setup
    for config in [CreativeHarnessConfig(), CreativeHarnessConfig(enabled=True)]:
        with pytest.raises(DomainError) as off:
            DynamicAgentService(k, config=config).register_revision(owner, *source, **args(0))
        assert off.value.code == "DYNAMIC_AGENTS_DISABLED"
    with k.db.uow(write=False) as repo:
        assert not repo.many("artifacts", {"work_id": wr["work_id"], "kind": "CREATIVE_DYNAMIC_INSTANCE"})
    for patch in [
        {"role_instructions": "", "requested_tools": []},
        {"requested_tools": ["world_accept"]},
        {"skill_instructions": args(0)["skill_instructions"] + " "},
        {"workshop_binding": {**args(0)["workshop_binding"], "org_id": str(uuid4())}},
        {"workshop_binding": {**args(0)["workshop_binding"], "employment_revision": 2}},
    ]:
        with pytest.raises(DomainError):
            svc.register_revision(owner, *source, **{**args(0), **patch})
    registered = svc.register_revision(owner, *source, **args(0))
    with pytest.raises(DomainError) as known_label:
        svc.register_revision(owner, *source, **{**args(1), "profile_version": args(0)["profile_version"]})
    assert known_label.value.code == "DYNAMIC_VERSION_CONFLICT"
    select = {
        "instance_ref": registered["instance_ref"],
        "revision_ref": registered["revision_ref"],
        "expected_epoch": 0,
        "idempotency_key": "set",
    }
    unauthorized = k.issue_grant(
        owner, wr["work_id"], role_id="writer", capabilities=["READ"], branch_ids=[wr["branch_id"]]
    )["principal"]
    with pytest.raises(DomainError):
        svc.select(unauthorized, wr, **select)
    sibling = k.fork(owner, wr, "CANDIDATE", "fork")
    with pytest.raises(DomainError):
        svc.select(owner, sibling, **select)
    alien = load_fixture(k)
    with pytest.raises(DomainError):
        svc.history(alien["principal"], alien["world_ref"], instance_ref=registered["instance_ref"])
    assert svc.select(owner, wr, **select)["state"] == "ACTIVE"
    with pytest.raises(DomainError) as other_role:
        svc.freeze(
            owner,
            wr,
            role_id="chief_editor",
            task_kind="editorial_decision",
            task_id=str(uuid4()),
            instance_ref=registered["instance_ref"],
        )
    assert other_role.value.code == "ROLE_MODE_DENIED"
    revoke(k, owner, args(0)["workshop_binding"]["installation_id"])
    with pytest.raises(DomainError):
        svc.freeze(
            owner,
            wr,
            role_id="evidence_maintainer",
            task_kind="summary",
            task_id=str(uuid4()),
            instance_ref=registered["instance_ref"],
        )


@pytest.mark.parametrize("uncertain_send", [False, True])
def test_expired_dynamic_lease_keeps_original_instructions_and_spend_fence(
    setup, delegated_site, uncertain_send, monkeypatch
):
    import time

    from test_harness_context import EnvelopeFixture

    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.delegation import provision_slot
    from novel_factory.application.dynamic_runtime import refresh_task, verify_task
    from novel_factory.application.queries import WorldQueryService
    from novel_factory.application.runtime_tasks import RuntimeTaskService
    from novel_factory.application.supervisor import RuntimeSupervisor
    from novel_factory.fixtures import point
    from novel_factory.infrastructure.database import tables
    from novel_factory.infrastructure.repository import KernelRepository

    k, wr, owner, source, svc, args, _ = setup
    delegation, submit, _, _, site_supervisor, _, _, original = delegated_site
    versions = [svc.register_revision(owner, *source, **args(i)) for i in (0, 1)]
    first, second = versions
    instance = first["instance_ref"]
    svc.select(
        owner,
        wr,
        instance_ref=instance,
        revision_ref=first["revision_ref"],
        expected_epoch=0,
        idempotency_key="one",
    )
    k.cancel_task(original.principal, *source, "close-source")
    adapter = EnvelopeFixture()
    route = site_supervisor.gateway.adapters["fixture"]
    monkeypatch.setattr(route, "generate", adapter.generate)
    gateway = AgentGateway(k, {"fixture": route}, creative_harness=svc.config)
    runtime = RuntimeTaskService(k, gateway)
    supervisor = RuntimeSupervisor(k, gateway)
    with k.db.uow(write=False) as repo:
        manifest = k.artifact(repo, original.manifest_ref, wr["work_id"])["payload"]
    frozen = runtime.define(
        owner,
        wr,
        role_id="evidence_maintainer",
        task_kind="summary",
        story_time=point(1),
        subject_ids=manifest["subject_ids"],
        predicates=manifest["predicates"],
        assignment="清点材料。",
        adapter_id="fixture",
        max_amount="0.10",
        token_budget=100000,
        dynamic_instance_ref=instance,
    )
    tid = frozen.spec["task_id"]
    budget = k.tasks.create_budget(wr["work_id"], "1")
    provision_slot(
        supervisor,
        owner,
        delegation.installation_id,
        scope_ref="recover.summary",
        task_template_id="summary.v1",
        frozen=frozen,
        budget_id=budget,
        reserve_amount="0.10",
        token_reservation=100000,
        accept_summary=True,
    )
    delegation.call(
        "nf_delegation_submit",
        {
            **submit,
            "operation_id": str(uuid4()),
            "budget_reservation_ref": str(uuid4()),
            "scope_ref": "recover.summary",
        },
    )
    leased = k.claim_task(frozen.principal, "first-worker", task_id=tid, lease_seconds=3)
    gen = leased["generation"]
    with k.db.uow(write=False) as repo:
        input_ref = frozen.agent_input["input_ref"]
        assignment = k.artifact(repo, input_ref, wr["work_id"])["payload"]["dynamic_assignment_ref"]
        persisted = repo.one("task_inputs", {"task_id": tid, "generation": gen})["input_hash"]
        config = repo.one("supervisor_jobs", {"task_id": tid})["config"]
    svc.select(
        owner,
        wr,
        instance_ref=instance,
        revision_ref=second["revision_ref"],
        expected_epoch=1,
        idempotency_key="two",
    )
    if uncertain_send:
        WorldQueryService(k).context_compile(
            frozen.principal,
            {
                "schema_version": "2.1",
                "task_id": tid,
                "generation": gen,
                "recipe_id": "MAINTAINER",
                "input_manifest_ref": frozen.manifest_ref,
                "knowledge_cut": {"plane": "WORLD"},
                "read_contract": {
                    "revision": {"mode": "FIXED", "world_ref": wr},
                    "process_policy": "ALLOW_PARTIAL",
                    "index_policy": "AUTHORITATIVE",
                    "max_wait_ms": 0,
                },
                "subject_time": {"kind": "POINT", "at": point(1)},
                "token_budget": 20000,
                "idempotency_key": "before-send",
            },
        )
        proof = refresh_task(k, frozen.principal, tid, gen)

        def qualify(conn):
            repo = KernelRepository(conn, tables)
            task = repo.one("tasks", {"task_id": tid})
            verify_task(k, repo, frozen.principal, task, proof)

        attempt = k.tasks.reserve_call(
            tid,
            gen,
            "first-worker",
            budget_id=budget,
            amount="0.1",
            token_amount=100000,
            call_key="supervised-candidate",
            request_hash="7" * 64,
            authorize_in_tx=qualify,
        )
        assert k.tasks.mark_call_sent(
            attempt["attempt_id"], worker_id="first-worker", authorize_in_tx=qualify
        )
    time.sleep(3.05)
    (recovery,) = k.tasks.recover_expired()
    assert recovery["task_id"] == tid and recovery["state"] == ("PAUSED" if uncertain_send else "READY")
    first_request = {
        "schema_version": "2.1",
        "task_id": tid,
        "generation": gen,
        "worker_id": "first-worker",
        "call_key": "supervised-candidate",
        **config["dispatch"],
    }
    with pytest.raises(DomainError):
        gateway.execute(frozen.principal, first_request)
    outcome = supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
    if uncertain_send:
        assert not adapter.calls
        with k.db.uow(write=False) as repo:
            (attempt,) = repo.many("call_attempts", {"task_id": tid})
            assert attempt["state"] == "UNKNOWN" and repo.one("tasks", {"task_id": tid})["state"] == "PAUSED"
        return
    assert outcome["status"] == "RESULT_STORED", outcome
    supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(adapter.calls) == 1
    system = adapter.calls[0].messages[0]["content"]
    assert args(0)["skill_instructions"] in system and args(1)["skill_instructions"] not in system
    with k.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": tid})
        assert task["state"] == "ACCEPTED" and task["generation"] > gen
        assert repo.one("task_inputs", {"task_id": tid, "generation": gen})["input_hash"] == persisted
        (lineage,) = repo.many("artifacts", {"work_id": wr["work_id"], "kind": "CREATIVE_DYNAMIC_GENERATION"})
        assert lineage["payload"]["lineage"]["frozen_ref"] == assignment
        assert lineage["payload"]["lineage"]["scope"]["generation"] == task["generation"]
