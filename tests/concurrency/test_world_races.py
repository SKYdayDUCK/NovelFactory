"""P1-02/08: real PostgreSQL competing world transactions and failure fences."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.world import state_at, resource_at
from novel_factory.fixtures import load_fixture, prepare_command, run_command, point, entity_value
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore

pytestmark = [pytest.mark.postgres, pytest.mark.concurrency]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def race_kernel(tmp_path_factory):
    url = os.environ.get("NF_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Real PostgreSQL is required; run through scripts/with_postgres.py")
    name = "nf_races_" + uuid4().hex[:16]
    admin = create_engine(make_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        assert int(conn.scalar(text("SHOW server_version_num"))) // 10000 == 17
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(url).set(database=name).render_as_string(hide_password=False)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                   env={**os.environ, "NF_DATABASE_URL": target, "DATABASE_URL": target}, check=True)
    db = Database(target, pool_size=8)
    kernel = Kernel(db, ObjectStore(tmp_path_factory.mktemp("race_objects")))
    yield kernel
    db.close()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def race_world(race_kernel):
    return load_fixture(race_kernel, "white_bridge")


def _gift(world, sword="sword", source="smith", target="protagonist", day=10):
    ids = world["ids"]
    return {"item_id": ids[sword], "from_id": ids[source], "to_id": ids[target],
            "transfer_kind": "GIFT", "at": point(day)}


def _transfer(world, source="warehouse", target="relief", amount="30", day=10):
    ids = world["ids"]
    return {"from_account": ids[source], "to_account": ids[target], "resource_id": ids["grain"],
            "quantity": amount, "unit": "bag", "at": point(day)}


def _prepare(kernel, world, template, args, mode="DEPENDENCY_GUARDED"):
    return prepare_command(kernel, world["principal"], world["world_ref"], template, args, mode=mode)


def _head(kernel, world):
    with kernel.db.uow(write=False) as repo:
        return kernel.head(repo, world["world_ref"]["work_id"], world["world_ref"]["branch_id"])


def _snapshot(kernel, wr):
    with kernel.db.uow(write=False) as repo:
        return kernel.revision(repo, wr)["snapshot"]


def _overlap(kernel, principal, first, second):
    """Hold the first branch transaction and observe the second PG lock wait.

    No SQLite/memory model or timer-only guess: pg_blocking_pids must identify
    two distinct backend processes contending on this actual branch write.
    """
    held, release = threading.Event(), threading.Event()
    def fault(stage):
        if stage == "after_events":
            held.set()
            if not release.wait(timeout=10):
                raise AssertionError("test did not release branch transaction")
    def accept(request, hook=None):
        try:
            return kernel.accept(principal, request, fault=hook)
        except DomainError as exc:
            return exc
    observed = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(accept, first, fault)
        if not held.wait(timeout=10):
            # Expose the actual underlying failure rather than time out twice.
            release.set()
            result = left.result(timeout=2)
            raise AssertionError(f"First world transaction did not reach injected point: {result}")
        right = pool.submit(accept, second)
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                with kernel.db.engine.connect() as conn:
                    rows = conn.execute(text("""SELECT pid, pg_blocking_pids(pid) AS blockers
                      FROM pg_stat_activity WHERE datname=current_database()
                      AND wait_event_type='Lock' AND query ILIKE '%nf_branches%'""")).mappings().all()
                observed = [dict(row) for row in rows if row["blockers"]]
                if observed:
                    break
                if right.done():
                    raise AssertionError(f"Second transaction did not wait for branch lock: {right.result()}")
                time.sleep(0.01)
            assert observed, "No real PostgreSQL lock contention was observed"
            assert all(row["pid"] not in row["blockers"] for row in observed)
        finally:
            release.set()
        return left.result(timeout=10), right.result(timeout=10), observed


def test_strict_head_competing_connections_allow_one(race_kernel, race_world):
    k, w = race_kernel, race_world
    a = _prepare(k, w, "TRANSFER_ITEM", _gift(w), mode="STRICT_HEAD")
    b = _prepare(k, w, "TRANSFER_ITEM", _gift(w, "sword2", "smith2", "friend"), mode="STRICT_HEAD")
    accepted, rejected, waits = _overlap(k, w["principal"], a["request"], b["request"])
    assert isinstance(accepted, dict)
    assert isinstance(rejected, DomainError) and rejected.code == "HEAD_CONFLICT"
    assert accepted["branch_seq"] == 1 and _head(k, w) == accepted["world_ref"]


def test_guarded_independent_items_keep_original_base(race_kernel, race_world):
    k, w = race_kernel, race_world
    a = _prepare(k, w, "TRANSFER_ITEM", _gift(w))
    b = _prepare(k, w, "TRANSFER_ITEM", _gift(w, "sword2", "smith2", "friend"))
    first, second, waits = _overlap(k, w["principal"], a["request"], b["request"])
    assert isinstance(first, dict) and isinstance(second, dict), (first, second)
    assert second["branch_seq"] == 2
    assert second["original_base_world_ref"] == second["validation_world_ref"] == w["world_ref"]
    assert second["parent_world_ref"] == first["world_ref"]
    snapshot = _snapshot(k, second["world_ref"])
    assert state_at(snapshot, w["ids"]["sword"], "owner", point(11))["data"] == entity_value(w["ids"]["protagonist"])
    assert state_at(snapshot, w["ids"]["sword2"], "owner", point(11))["data"] == entity_value(w["ids"]["friend"])


def test_guarded_independent_resources_share_one_world_chain(race_kernel, race_world):
    k, w = race_kernel, race_world
    a = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w))
    b = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w, "reserve", "recipient"))
    first, second, _ = _overlap(k, w["principal"], a["request"], b["request"])
    assert isinstance(first, dict) and isinstance(second, dict), (first, second)
    snapshot = _snapshot(k, second["world_ref"])
    assert resource_at(snapshot, w["ids"]["warehouse"], w["ids"]["grain"], "bag")["quantity"] == "70"
    assert resource_at(snapshot, w["ids"]["reserve"], w["ids"]["grain"], "bag")["quantity"] == "70"


def test_shared_resource_rejects_overallocation(race_kernel, race_world):
    k, w = race_kernel, race_world
    a = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w, amount="80"))
    b = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w, target="recipient", amount="80"))
    first, second, _ = _overlap(k, w["principal"], a["request"], b["request"])
    assert isinstance(first, dict)
    assert isinstance(second, DomainError) and second.code == "DEPENDENCY_CONFLICT"
    snapshot = _snapshot(k, first["world_ref"])
    amounts = [resource_at(snapshot, w["ids"][a], w["ids"]["grain"], "bag")["quantity"] for a in ("warehouse", "relief", "recipient")]
    assert amounts == ["20", "80", "0"]


def test_write_skew_two_guards_cannot_both_leave(race_kernel, race_world):
    k, w = race_kernel, race_world
    a, b = [_prepare(k, w, "GUARD_LEAVE", {"guard_id": w["ids"][guard], "region_id": w["ids"]["north"], "at": point(10)}) for guard in ("guard1", "guard2")]
    first, second, _ = _overlap(k, w["principal"], a["request"], b["request"])
    assert isinstance(first, dict)
    assert isinstance(second, DomainError) and second.code == "DEPENDENCY_CONFLICT"
    snapshot = _snapshot(k, first["world_ref"])
    assert [state_at(snapshot, w["ids"][guard], "on_duty", point(11))["data"]["value"] for guard in ("guard1", "guard2")] == [False, True]


def test_negative_range_phantom_invalidates_clear_route(race_kernel, race_world):
    k, w = race_kernel, race_world
    route = _prepare(k, w, "PASS_CLEAR_ROUTE", {"traveler_id": w["ids"]["traveler"], "region_id": w["ids"]["north"], "destination_id": w["ids"]["south"], "at": point(10)})
    blocker = _prepare(k, w, "SET_RELATION", {"subject_id": w["ids"]["north"], "predicate": "blocked_by", "object_value": entity_value(w["ids"]["blocker"]), "at": point(5)}, mode="STRICT_HEAD")
    with k.db.uow(write=False) as repo:
        cert = k.artifact(repo, route["request"]["acceptance_certificate_ref"], w["world_ref"]["work_id"])["payload"]
        token = next(t for t in cert["read_scope_tokens"] if t["scope_key"] == "PREDICATE_RANGE:blocked_by")
        assert token["version"] == 0
    first, second, _ = _overlap(k, w["principal"], blocker["request"], route["request"])
    assert isinstance(first, dict)
    assert isinstance(second, DomainError) and second.code == "DEPENDENCY_CONFLICT"
    with k.db.uow(write=False) as repo:
        assert repo.scope_map(w["world_ref"]["work_id"], w["world_ref"]["branch_id"])["PREDICATE_RANGE:blocked_by"] == 1
    assert state_at(_snapshot(k, first["world_ref"]), w["ids"]["traveler"], "location", point(11))["data"] == entity_value(w["ids"]["north"])


def test_after_sample_change_does_not_refresh_certificate_tokens(race_kernel, race_world):
    k, w = race_kernel, race_world
    a = _prepare(k, w, "TRANSFER_ITEM", _gift(w))
    b = _prepare(k, w, "TRANSFER_ITEM", _gift(w, target="friend"))
    prepare = {**a["prepare_request"], "idempotency_key": "sample-race:" + str(uuid4())}
    cert_ref = k.prepare(w["principal"], prepare, after_sample=lambda: k.accept(w["principal"], b["request"]))
    request = {**a["request"], "acceptance_certificate_ref": cert_ref, "idempotency_key": "sample-accept:" + str(uuid4())}
    with k.db.uow(write=False) as repo:
        cert = k.artifact(repo, cert_ref, w["world_ref"]["work_id"])["payload"]
        assert cert["validated_world_ref"] == w["world_ref"]
        assert next(t for t in cert["read_scope_tokens"] if t["scope_key"] == "ENTITY:" + w["ids"]["sword"])["version"] == 0
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], request)
    assert exc.value.code == "DEPENDENCY_CONFLICT"
    assert state_at(_snapshot(k, _head(k, w)), w["ids"]["sword"], "owner", point(11))["data"] == entity_value(w["ids"]["friend"])


def test_current_grant_and_approval_revocation_block_accept(race_kernel, race_world):
    k, w = race_kernel, race_world
    delegate = k.issue_grant(w["principal"], w["world_ref"]["work_id"], role_id="control_operator", capabilities=ADMIN_CAPABILITIES, planes=["WORLD", "BELIEF"], max_story_time=point(100))
    sub = {**w, "principal": delegate["principal"]}
    candidate = _prepare(k, sub, "TRANSFER_ITEM", _gift(w))
    k.revoke_grant(w["principal"], delegate["principal"].grant_id)
    with pytest.raises(DomainError) as exc:
        k.accept(delegate["principal"], candidate["request"])
    assert exc.value.code == "VIEW_DENIED"
    other = _prepare(k, w, "TRANSFER_ITEM", _gift(w))
    k.revoke_approval(w["principal"], w["world_ref"]["work_id"], other["proposal"]["approval_id"])
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], other["request"])
    assert exc.value.code == "APPROVAL_STALE"
    assert _head(k, w) == w["world_ref"]


def test_expired_and_unissued_certificates_are_rejected(race_kernel, race_world):
    k, w = race_kernel, race_world
    previous = getattr(k, "certificate_ttl_seconds", 300)
    k.certificate_ttl_seconds = 0
    try:
        expired = _prepare(k, w, "TRANSFER_ITEM", _gift(w))
    finally:
        k.certificate_ttl_seconds = previous
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], expired["request"])
    assert exc.value.code == "STALE_CERTIFICATE"
    forged = deepcopy(expired["request"])
    forged["acceptance_certificate_ref"]["artifact_revision_id"] = str(uuid4())
    forged["idempotency_key"] = "forged:" + str(uuid4())
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], forged)
    assert exc.value.code == "VIEW_DENIED"


def test_interpretation_epoch_invalidates_prepared_acceptance(race_kernel, race_world):
    k, w = race_kernel, race_world
    prepared = _prepare(k, w, "TRANSFER_ITEM", _gift(w))
    # Isolated operator-side semantic deployment fault injection. P1 does not
    # expose arbitrary interpretation editing as an Agent API.
    with k.db.uow() as repo:
        work = repo.one("works", {"work_id": w["world_ref"]["work_id"]}, lock="update")
        repo.change("works", {"work_id": work["work_id"]}, {"interpretation_epoch": work["interpretation_epoch"] + 1})
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], prepared["request"])
    assert exc.value.code in {"STALE_CERTIFICATE", "DEPENDENCY_CONFLICT"}
    assert _head(k, w) == w["world_ref"]


def test_same_key_retry_and_new_key_do_not_repeat_effects(race_kernel, race_world):
    k, w = race_kernel, race_world
    prepared = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w, amount="25"))
    receipt = k.accept(w["principal"], prepared["request"])
    assert k.accept(w["principal"], prepared["request"]) == receipt
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], {**prepared["request"], "idempotency_key": "different:" + str(uuid4())})
    assert exc.value.code == "ALREADY_ACCEPTED"
    changed = deepcopy(prepared["request"]); changed["generation"] += 1
    with pytest.raises(DomainError) as exc:
        k.accept(w["principal"], changed)
    assert exc.value.code in {"IDEMPOTENCY_MISMATCH", "IDEMPOTENCY_CONFLICT"}
    assert resource_at(_snapshot(k, _head(k, w)), w["ids"]["warehouse"], w["ids"]["grain"], "bag")["quantity"] == "75"
    # A completed task cannot be cancelled; later approval revocation still
    # cannot erase the original successful command's historical receipt.
    with pytest.raises(DomainError) as cancellation:
        k.cancel_task(w["principal"], prepared["task"]["task_id"], prepared["task"]["generation"], "cancel-after-commit")
    assert cancellation.value.code == "TASK_ALREADY_ACCEPTED"
    k.revoke_approval(w["principal"], w["world_ref"]["work_id"], prepared["proposal"]["approval_id"])
    assert k.accept(w["principal"], prepared["request"]) == receipt


def test_direct_sql_cannot_rewrite_events_or_delete_scope_tokens(race_kernel, race_world):
    k, w = race_kernel, race_world
    accepted = run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM", _gift(w))
    event_id = accepted["receipt"]["accepted_event_refs"][0]["event_version_id"]
    for statement, params in [
        ("UPDATE nf_events SET payload=payload WHERE event_version_id=:id", {"id": event_id}),
        ("DELETE FROM nf_events WHERE event_version_id=:id", {"id": event_id}),
        ("UPDATE nf_revisions SET snapshot=snapshot WHERE world_revision_id=:id", {"id": accepted["world_ref"]["world_revision_id"]}),
        ("DELETE FROM nf_scopes WHERE work_id=:id", {"id": w["world_ref"]["work_id"]}),
    ]:
        with pytest.raises(DBAPIError):
            with k.db.engine.begin() as conn:
                conn.execute(text(statement), params)
    assert _head(k, w) == accepted["world_ref"]


def _kill_world_accept(url, object_root, principal, request, stage):
    kernel = Kernel(Database(url), ObjectStore(object_root))
    def fault(point):
        if point == stage:
            os._exit(81)
    kernel.accept(principal, request, fault=fault)


@pytest.mark.fault
@pytest.mark.parametrize("stage", ["after_events", "after_commit"])
def test_world_transaction_process_crash_and_retry(race_kernel, race_world, stage):
    k, w = race_kernel, race_world
    prepared = _prepare(k, w, "RESOURCE_TRANSFER", _transfer(w, amount="25"))
    url = k.db.engine.url.render_as_string(hide_password=False)
    child = multiprocessing.get_context("spawn").Process(target=_kill_world_accept,
        args=(url, str(k.objects.root), w["principal"], prepared["request"], stage))
    child.start(); child.join(timeout=15)
    assert child.exitcode == 81
    before_retry = _head(k, w)
    if stage == "after_events":
        assert before_retry == w["world_ref"]
        with k.db.uow(write=False) as repo:
            assert repo.many("events", {"work_id": w["world_ref"]["work_id"]}) == []
    else:
        assert before_retry != w["world_ref"]
    receipt = k.accept(w["principal"], prepared["request"])
    assert receipt["branch_seq"] == 1
    assert resource_at(_snapshot(k, receipt["world_ref"]), w["ids"]["warehouse"], w["ids"]["grain"], "bag")["quantity"] == "75"
    if stage == "after_commit":
        assert receipt["world_ref"] == before_retry


def test_missing_cognitive_evidence_cannot_become_formal_knowledge(race_kernel, race_world):
    k, w = race_kernel, race_world
    evidence = {"kind": "EVENT", "world_ref": w["world_ref"],
                "event_ref": {"occurrence_id": str(uuid4()), "event_version_id": str(uuid4())}}
    args = {"actor_id": w["ids"]["protagonist"], "record_kind": "BELIEF", "epistemic_mode": "OBSERVED",
            "statement": "The isolated test supplies a nonexistent source.", "evidence_refs": [evidence], "at": point(10)}
    with pytest.raises(DomainError) as exc:
        prepared = _prepare(k, w, "RECORD_COGNITION", args, mode="STRICT_HEAD")
        k.accept(w["principal"], prepared["request"])
    assert exc.value.code in {"INVALID_EVIDENCE", "VIEW_DENIED", "INVALID_ARGUMENT", "HARD_RULE_VIOLATION"}
    assert _head(k, w) == w["world_ref"]


def test_repair_cannot_reference_nonexistent_rules_or_adjudication(race_kernel, race_world):
    k, w = race_kernel, race_world
    accepted = run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM", _gift(w))
    repair = k.fork(w["principal"], accepted["world_ref"], "REPAIR", "repair-missing-source")
    with k.db.uow(write=False) as repo:
        selected = k.revision(repo, repair)["selection"]
        original = repo.one("events", {"event_version_id": next(iter(selected.values()))})["payload"]
    replacement = deepcopy(original)
    replacement["event_version_id"] = str(uuid4())
    replacement["representation_id"] = str(uuid4())
    fake = {"artifact_id": str(uuid4()), "artifact_revision_id": str(uuid4()), "sha256": "a" * 64}
    replacement["ruleset_ref"] = fake
    replacement["adjudication_ref"] = fake
    reviewer = k.issue_grant(w["principal"], w["world_ref"]["work_id"], role_id="control_operator", branch_ids=["*"],
        capabilities=ADMIN_CAPABILITIES, planes=["WORLD"], max_story_time=point(1000))["principal"]
    args = {"occurrence_id": original["occurrence_id"], "replacement_event": replacement, "reason": "Verify that repair cannot invent source records"}
    validate_contract("TemplateCommand", {"template_id": "REPAIR_EVENT", "args": args}, "commands")
    with pytest.raises(DomainError) as exc:
        prepared = prepare_command(k, w["principal"], repair, "REPAIR_EVENT", args, reviewer=reviewer)
        k.accept(w["principal"], prepared["request"])
    assert exc.value.code in {"INVALID_EVIDENCE", "VIEW_DENIED", "INVALID_ARGUMENT", "HARD_RULE_VIOLATION"}
    with k.db.uow(write=False) as repo:
        assert k.head(repo, repair["work_id"], repair["branch_id"]) == repair


def test_task_authorization_is_rechecked_inside_mutation_transaction(race_kernel, race_world, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from novel_factory.domain.canonical import fingerprint
    k, w = race_kernel, race_world
    delegate = k.issue_grant(w["principal"], w["world_ref"]["work_id"], role_id="control_operator", capabilities=ADMIN_CAPABILITIES,
        planes=["WORLD"], max_story_time=point(100))["principal"]
    spec = {"schema_version": "2.0", "task_id": str(uuid4()), "generation": 0, "role_id": "control_operator", "task_kind": "authorization_race",
        "state": "READY", "world_ref": w["world_ref"], "story_time": point(10), "context_bundle_id": None, "policy_version": "nf-policy-1",
        "dependency_fingerprint": None, "runtime_contract_version": fingerprint({"test": "authorization-race"}), "capability_grant_id": delegate.grant_id,
        "input_artifacts": [], "budget": {"currency": "USD", "max_amount": "1", "token_budget": 100},
        "wall_deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    # Revoke from a second transaction after validation and after opening the
    # real write transaction, immediately before its live authorization fence.
    original = k._create_checked_task
    revoked = []

    def revoke_between_precheck_and_mutation(repo, *args, **kwargs):
        assert repo.conn.in_transaction()
        k.revoke_grant(w["principal"], delegate.grant_id)
        revoked.append(delegate.grant_id)
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(k, "_create_checked_task", revoke_between_precheck_and_mutation)
    with pytest.raises(DomainError) as exc:
        k.create_task(delegate, spec, "auth-before-mutation")
    assert revoked == [delegate.grant_id]
    assert exc.value.code == "VIEW_DENIED"
    with k.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": spec["task_id"]}) is None


def _reviewer(kernel, world):
    return kernel.issue_grant(world["principal"], world["world_ref"]["work_id"], role_id="control_operator", branch_ids=["*"],
        capabilities=ADMIN_CAPABILITIES, planes=["WORLD"], max_story_time=point(1000))["principal"]


def _replacement(kernel, wr, occurrence_id=None):
    with kernel.db.uow(write=False) as repo:
        selected = kernel.revision(repo, wr)["selection"]
        version = selected[occurrence_id] if occurrence_id else next(iter(selected.values()))
        event = deepcopy(repo.one("events", {"event_version_id": version})["payload"])
    event["event_version_id"] = str(uuid4())
    event["representation_id"] = str(uuid4())
    return event


def test_repair_revalidates_later_closed_command_preconditions(race_kernel, race_world):
    k, w = race_kernel, race_world
    gift = run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM", _gift(w))
    loan_args = {**_gift(w, source="protagonist", target="friend", day=20), "transfer_kind": "LOAN"}
    loan = run_command(k, w["principal"], gift["world_ref"], "TRANSFER_ITEM", loan_args)
    repair = k.fork(w["principal"], loan["world_ref"], "REPAIR", "repair-causal-recheck")
    occurrence = gift["receipt"]["accepted_event_refs"][0]["occurrence_id"]
    replacement = _replacement(k, repair, occurrence)
    # D10 GIFT -> LOAN: original owner remains smith. The unchanged D20 LOAN
    # from protagonist now violates the registered owner-only lending rule.
    replacement["effects"] = [e for e in replacement["effects"] if e.get("predicate") != "legal_owner"]
    validate_contract("AcceptedEvent", replacement, "2.0")
    args = {"occurrence_id": occurrence, "replacement_event": replacement,
            "reason": "Change early gift to loan without silently keeping an impossible later owner-only loan"}
    validate_contract("TemplateCommand", {"template_id": "REPAIR_EVENT", "args": args}, "commands")
    reviewer = _reviewer(k, w)
    with pytest.raises(DomainError) as exc:
        run_command(k, w["principal"], repair, "REPAIR_EVENT", args, reviewer=reviewer)
    assert exc.value.code in {"REPAIR_INCOMPLETE", "RESOURCE_CONFLICT", "DEPENDENCY_CONFLICT", "HARD_RULE_VIOLATION"}
    with k.db.uow(write=False) as repo:
        assert k.head(repo, repair["work_id"], repair["branch_id"]) == repair
        assert k.head(repo, w["world_ref"]["work_id"]) == loan["world_ref"]


def test_promotion_rechecks_current_repair_approval(race_kernel, race_world):
    k, w = race_kernel, race_world
    gift = run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM", _gift(w))
    repair = k.fork(w["principal"], gift["world_ref"], "REPAIR", "repair-promotion-revocation")
    replacement = _replacement(k, repair)
    reviewer = _reviewer(k, w)
    prepared = prepare_command(k, w["principal"], repair, "REPAIR_EVENT",
        {"occurrence_id": replacement["occurrence_id"], "replacement_event": replacement,
         "reason": "Revalidate equivalent history before promotion"}, reviewer=reviewer)
    repaired = k.accept(w["principal"], prepared["request"])
    # Candidate history remains immutable, but revoked approval cannot authorize
    # the separate, later act of moving the formal world pointer.
    k.revoke_approval(w["principal"], w["world_ref"]["work_id"], prepared["request"]["approval_refs"][0])
    with pytest.raises(DomainError) as exc:
        k.promote(w["principal"], gift["world_ref"], repaired["world_ref"], "promote-revoked")
    assert exc.value.code in {"APPROVAL_STALE", "REPAIR_INCOMPLETE", "VIEW_DENIED"}
    with k.db.uow(write=False) as repo:
        assert k.head(repo, w["world_ref"]["work_id"]) == gift["world_ref"]
        assert k.head(repo, repair["work_id"], repair["branch_id"]) == repaired["world_ref"]


def test_promotion_cas_preserves_new_canon_and_repair_history(race_kernel, race_world):
    k, w = race_kernel, race_world
    gift = run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM", _gift(w))
    repair = k.fork(w["principal"], gift["world_ref"], "REPAIR", "repair-promotion-cas")
    replacement = _replacement(k, repair)
    repaired = run_command(k, w["principal"], repair, "REPAIR_EVENT",
        {"occurrence_id": replacement["occurrence_id"], "replacement_event": replacement,
         "reason": "Versioned repair for pointer competition"}, reviewer=_reviewer(k, w))
    advanced = run_command(k, w["principal"], gift["world_ref"], "TRANSFER_ITEM", _gift(w, "sword2", "smith2", "friend", day=11))
    with pytest.raises(DomainError) as exc:
        k.promote(w["principal"], gift["world_ref"], repaired["world_ref"], "promote-old-canon")
    assert exc.value.code == "HEAD_CONFLICT"
    with pytest.raises(DomainError) as exc:
        k.promote(w["principal"], advanced["world_ref"], repaired["world_ref"], "promote-rebased-without-review")
    assert exc.value.code == "HEAD_CONFLICT"
    with k.db.uow(write=False) as repo:
        assert k.head(repo, w["world_ref"]["work_id"]) == advanced["world_ref"]
        assert k.head(repo, repair["work_id"], repair["branch_id"]) == repaired["world_ref"]
