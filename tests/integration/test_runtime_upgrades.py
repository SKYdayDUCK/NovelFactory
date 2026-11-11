"""P4-02 compatible releases, independent approval and immutable model inputs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.upgrades import RuntimeUpgradeService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point, prepare_command, run_command
from novel_factory.infrastructure.objects import ObjectStoreError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def upgrade_request(kernel, world):
    wr = world["world_ref"]
    with kernel.db.uow(write=False) as repo:
        work = repo.one("works", {"work_id": wr["work_id"]})
        rules = deepcopy(kernel.artifact(repo, work["rules_ref"], wr["work_id"])["payload"])
        interpretation = deepcopy(kernel.artifact(repo, work["interpretation_ref"], wr["work_id"])["payload"])
    rules["version"] = "nf-world-rules-compatible-package-2"
    interpretation["package_release"] = "reviewed-compatible-package-2"
    return {"schema_version": "2.1", "world_ref": wr, "rules": rules,
            "interpretation": interpretation, "idempotency_key": "upgrade-prepare"}


def approved_package(kernel, world):
    service = RuntimeUpgradeService(kernel)
    request = upgrade_request(kernel, world)
    package = service.prepare(world["principal"], request)
    reviewer = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
                                  role_id="reviewer", capabilities=["READ", "APPROVE"], planes=["WORLD"], max_story_time=point(100))
    approval = service.review(reviewer["principal"], {"schema_version": "2.1", "world_ref": world["world_ref"],
                    "package_ref": package["package_ref"], "decision": "APPROVE",
                    "notes": "Reviewed exact historical replay and identical semantic fields.", "idempotency_key": "upgrade-review"})
    activation = {"schema_version": "2.1", "world_ref": world["world_ref"],
                  "package_ref": package["package_ref"], "approval_ref": approval, "idempotency_key": "upgrade-activate"}
    return service, activation, reviewer


def test_upgrade_appends_revision_preserves_history_and_fences_old_certificate(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    first = run_command(kernel, world["principal"], world["world_ref"], "TRANSFER_ITEM", {
        "item_id": world["ids"]["sword"], "from_id": world["ids"]["smith"], "to_id": world["ids"]["protagonist"],
        "transfer_kind": "GIFT", "at": point(10)})
    world = {**world, "world_ref": first["world_ref"]}
    old = prepare_command(kernel, world["principal"], world["world_ref"], "TRANSFER_ITEM", {
        "item_id": world["ids"]["sword2"], "from_id": world["ids"]["smith2"], "to_id": world["ids"]["friend"],
        "transfer_kind": "GIFT", "at": point(11)}, mode="DEPENDENCY_GUARDED")
    with kernel.db.uow(write=False) as repo:
        before = kernel.revision(repo, world["world_ref"])
        events = repo.many("events", {"work_id": world["world_ref"]["work_id"]})
    service, activation, _ = approved_package(kernel, world)
    receipt = service.activate(world["principal"], activation)
    assert receipt == service.activate(world["principal"], activation)
    assert receipt["new_world_effects"] == 0
    with kernel.db.uow(write=False) as repo:
        after = kernel.revision(repo, receipt["world_ref"])
        assert after["selection"] == before["selection"] and after["snapshot"] == before["snapshot"]
        assert after["interpretation_ref"] != before["interpretation_ref"]
        assert kernel.revision(repo, world["world_ref"]) == before
        assert repo.many("events", {"work_id": world["world_ref"]["work_id"]}) == events
    with pytest.raises(DomainError) as stale:
        kernel.accept(world["principal"], old["request"])
    assert stale.value.code in {"STALE_CERTIFICATE", "DEPENDENCY_CONFLICT"}
    assert kernel.accept(world["principal"], first["request"]) == first["receipt"]
    new = run_command(kernel, world["principal"], receipt["world_ref"], "TRANSFER_ITEM", {
        "item_id": world["ids"]["sword2"], "from_id": world["ids"]["smith2"], "to_id": world["ids"]["friend"],
        "transfer_kind": "GIFT", "at": point(11)})
    assert new["world_ref"] != receipt["world_ref"]


@pytest.mark.parametrize("field", ["resource_floor", "calendar", "reducer"])
def test_unproven_semantic_upgrade_is_rejected(kernel_env, field):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    request = upgrade_request(kernel, world)
    if field == "resource_floor":
        request["rules"][field] = "3"
    else:
        request["interpretation"][field] = "unproven-v2"
    with pytest.raises(DomainError) as error:
        RuntimeUpgradeService(kernel).prepare(world["principal"], request)
    assert error.value.code == "UPGRADE_REQUIRES_REPAIR"


def test_upgrade_requires_independent_active_reviewer_and_current_head(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    service = RuntimeUpgradeService(kernel)
    prepared = service.prepare(world["principal"], upgrade_request(kernel, world))
    with pytest.raises(DomainError) as own:
        service.review(world["principal"], {"schema_version": "2.1", "world_ref": world["world_ref"],
            "package_ref": prepared["package_ref"], "decision": "APPROVE", "notes": "Self-review attempt", "idempotency_key": "self"})
    assert own.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    service, activation, reviewer = approved_package(kernel, world)
    kernel.revoke_grant(world["principal"], reviewer["principal"].grant_id)
    with pytest.raises(DomainError) as revoked:
        service.activate(world["principal"], activation)
    assert revoked.value.code == "VIEW_DENIED"


def test_upgrade_does_not_move_head_after_concurrent_world_accept(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    service, activation, _ = approved_package(kernel, world)
    newer = run_command(kernel, world["principal"], world["world_ref"], "TRANSFER_ITEM", {
        "item_id": world["ids"]["sword"], "from_id": world["ids"]["smith"], "to_id": world["ids"]["friend"],
        "transfer_kind": "GIFT", "at": point(10)})
    with pytest.raises(DomainError) as stale:
        service.activate(world["principal"], activation)
    assert stale.value.code == "HEAD_CONFLICT"
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, world["world_ref"]["work_id"]) == newer["world_ref"]


def test_model_switch_preserves_frozen_inputs_and_rejects_route_reuse(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapters = {"model-v1": SimpleNamespace(configuration={"model": "fixture-v1", "execution_kind": "TEST_FIXTURE"}),
                "model-v2": SimpleNamespace(configuration={"model": "fixture-v2", "execution_kind": "TEST_FIXTURE"})}
    service = RuntimeUpgradeService(kernel, adapters=adapters)
    request = {"schema_version": "2.1", "world_ref": world["world_ref"], "adapter_id": "model-v1", "expected_ref": None, "idempotency_key": "model-first"}
    selected = service.select_model(world["principal"], request)
    gateway = AgentGateway(kernel, adapters)
    first = gateway.prepare_input(world["principal"], world["world_ref"], role_id="writer", task_kind="scene_draft", assignment="Fixture only")
    service.select_model(world["principal"], {**request, "adapter_id": "model-v2", "expected_ref": selected, "idempotency_key": "model-second"})
    second = gateway.prepare_input(world["principal"], world["world_ref"], role_id="writer", task_kind="scene_draft", assignment="Fixture only")
    with kernel.db.uow(write=False) as repo:
        old_input = kernel.artifact(repo, first["input_ref"], world["world_ref"]["work_id"])["payload"]
        new_input = kernel.artifact(repo, second["input_ref"], world["world_ref"]["work_id"])["payload"]
        selected2 = repo.one("selections", {"work_id": world["world_ref"]["work_id"], "selection_kind": "MODEL_ROUTE"})["artifact_ref"]
    assert old_input["adapter_id"] == "model-v1" and new_input["adapter_id"] == "model-v2"
    assert old_input["model_config_sha256"] != new_input["model_config_sha256"]
    adapters["model-v1"].configuration = {"model": "silently-changed"}
    with pytest.raises(DomainError) as changed:
        service.select_model(world["principal"], {**request, "expected_ref": selected2, "idempotency_key": "bad-reuse"})
    assert changed.value.code == "MODEL_CONFIG_CHANGED"


@pytest.mark.parametrize("scope", ["actor", "story_horizon"])
def test_runtime_upgrade_rejects_actor_or_insufficient_story_horizon(kernel_env, scope):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    grant = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="character_actor" if scope == "actor" else "control_operator",
        actor_id=world["ids"]["protagonist"] if scope == "actor" else None,
        capabilities=["READ"] if scope == "actor" else ["ADMIN", "READ"],
        planes=["BELIEF"] if scope == "actor" else ["WORLD"],
        max_story_time=point(100) if scope == "actor" else point(0))
    with pytest.raises(DomainError) as denied:
        RuntimeUpgradeService(kernel).prepare(grant["principal"], upgrade_request(kernel, world))
    assert denied.value.code == "VIEW_DENIED"


@contextmanager
def unavailable_object(store, ref, damage):
    """Damage only generated fixture bytes, then restore shared test objects."""
    path = store.path(ref["sha256"])
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"damaged fixture object")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        assert store.put(original).sha256 == ref["sha256"]


@pytest.mark.parametrize("source", ["package_ref", "approval_ref", "source_rules_ref", "source_interpretation_ref"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_upgrade_does_not_activate_with_unavailable_evidence(kernel_env, source, damage):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    service, activation, _ = approved_package(kernel, world)
    if source in activation:
        ref = activation[source]
    else:
        with kernel.db.uow(write=False) as repo:
            package = kernel.artifact(repo, activation["package_ref"], world["world_ref"]["work_id"])["payload"]
            ref = package[source]
    with unavailable_object(kernel.objects, ref, damage):
        with pytest.raises(ObjectStoreError) as error:
            service.activate(world["principal"], activation)
        assert error.value.code == ("OBJECT_MISSING" if damage == "missing" else "OBJECT_CORRUPT")
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, world["world_ref"]["work_id"]) == world["world_ref"]
            assert not repo.many("receipts", {"work_id": world["world_ref"]["work_id"], "command_type": "RUNTIME_UPGRADE"})
    # A repaired object resumes the same reviewed operation; no alternate
    # approval or bypass is required to complete the original request.
    assert service.activate(world["principal"], activation)["new_world_effects"] == 0


@pytest.mark.parametrize("source", ["rules_ref", "interpretation_ref"])
def test_upgrade_prepare_requires_durable_source_artifacts(kernel_env, source):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    request = upgrade_request(kernel, world)
    with kernel.db.uow(write=False) as repo:
        ref = repo.one("works", {"work_id": world["world_ref"]["work_id"]})[source]
    with unavailable_object(kernel.objects, ref, "missing"):
        with pytest.raises(ObjectStoreError) as error:
            RuntimeUpgradeService(kernel).prepare(world["principal"], request)
        assert error.value.code == "OBJECT_MISSING"
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("artifacts", {"work_id": world["world_ref"]["work_id"], "kind": "RUNTIME_UPGRADE_PACKAGE"})


def test_runtime_prepare_and_review_are_idempotent_under_concurrent_retries(kernel_env, monkeypatch):
    from novel_factory.application import upgrades
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    service = RuntimeUpgradeService(kernel)
    request = upgrade_request(kernel, world)
    replay_barrier = Barrier(3)
    original_replay = upgrades.replay

    def concurrent_replay(*args, **kwargs):
        replay_barrier.wait(timeout=15)
        return original_replay(*args, **kwargs)

    monkeypatch.setattr(upgrades, "replay", concurrent_replay)
    with ThreadPoolExecutor(max_workers=3) as pool:
        packages = list(pool.map(lambda _: service.prepare(world["principal"], request), range(3)))
    assert packages[0] == packages[1] == packages[2]
    reviewer = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="reviewer", capabilities=["READ", "APPROVE"], planes=["WORLD"], max_story_time=point(100))["principal"]
    review = {"schema_version": "2.1", "world_ref": world["world_ref"], "package_ref": packages[0]["package_ref"],
              "decision": "APPROVE", "notes": "Exact package review.", "idempotency_key": "review-racing"}
    review_barrier = Barrier(3)

    def concurrent_review(_):
        review_barrier.wait(timeout=15)
        return service.review(reviewer, review)

    with ThreadPoolExecutor(max_workers=3) as pool:
        approvals = list(pool.map(concurrent_review, range(3)))
    assert approvals[0] == approvals[1] == approvals[2]
    with kernel.db.uow(write=False) as repo:
        for kind in ["RUNTIME_UPGRADE_PACKAGE", "RUNTIME_UPGRADE_APPROVAL"]:
            assert len(repo.many("artifacts", {"work_id": world["world_ref"]["work_id"], "kind": kind})) == 1
    with pytest.raises(DomainError) as mismatch:
        service.review(reviewer, {**review, "notes": "Different content under the same key."})
    assert mismatch.value.code == "IDEMPOTENCY_MISMATCH"


def test_model_route_mutations_require_canonical_current_cut_and_keep_prior_receipt(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapters = {name: SimpleNamespace(configuration={"model": name, "execution_kind": "TEST_FIXTURE"})
                for name in ["fixture-v1", "fixture-v2"]}
    service = RuntimeUpgradeService(kernel, adapters=adapters)
    first = {"schema_version": "2.1", "world_ref": world["world_ref"], "adapter_id": "fixture-v1",
             "expected_ref": None, "idempotency_key": "route-first"}
    selected = service.select_model(world["principal"], first)
    sandbox = kernel.fork(world["principal"], world["world_ref"], "CANDIDATE", "runtime-sandbox")
    scoped = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="control_operator", branch_ids=[sandbox["branch_id"]], capabilities=["READ", "ADMIN"],
        planes=["WORLD"], max_story_time=point(100))["principal"]
    change = {**first, "adapter_id": "fixture-v2", "expected_ref": selected, "idempotency_key": "route-change"}
    with pytest.raises(DomainError) as denied:
        service.select_model(scoped, {**change, "world_ref": sandbox})
    assert denied.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as denied:
        service.selected_model(scoped, sandbox)
    assert denied.value.code == "VIEW_DENIED"
    newer = run_command(kernel, world["principal"], world["world_ref"], "TRANSFER_ITEM", {
        "item_id": world["ids"]["sword"], "from_id": world["ids"]["smith"], "to_id": world["ids"]["friend"],
        "transfer_kind": "GIFT", "at": point(10)})
    with pytest.raises(DomainError) as stale:
        service.select_model(world["principal"], change)
    assert stale.value.code == "HEAD_CONFLICT"
    assert service.select_model(world["principal"], first) == selected
    assert service.selected_model(world["principal"], newer["world_ref"]) == "fixture-v1"
    service.select_model(world["principal"], {**change, "world_ref": newer["world_ref"]})
    assert service.selected_model(world["principal"], newer["world_ref"]) == "fixture-v2"


def test_selected_model_rejects_corrupt_route_evidence(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    adapter = SimpleNamespace(configuration={"model": "fixture-v1", "execution_kind": "TEST_FIXTURE"})
    service = RuntimeUpgradeService(kernel, adapters={"fixture-v1": adapter})
    request = {"schema_version": "2.1", "world_ref": world["world_ref"], "adapter_id": "fixture-v1",
               "expected_ref": None, "idempotency_key": "route-integrity"}
    selected = service.select_model(world["principal"], request)
    with unavailable_object(kernel.objects, selected, "corrupt"):
        with pytest.raises(ObjectStoreError) as error:
            service.selected_model(world["principal"], world["world_ref"])
        assert error.value.code == "OBJECT_CORRUPT"
