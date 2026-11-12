"""Exercise process-local priming against a persisted fixed revision."""
import pytest

from novel_factory.application.kernel import Kernel
from novel_factory.domain import world as reducer
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_independent_proposals_share_only_the_proved_parent(kernel_env, monkeypatch):
    monkeypatch.setenv("NF_SCION_REPLAY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_LAZY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_PRIME", "1")
    seeder = Kernel(kernel_env["db"], kernel_env["store"])
    world = load_fixture(seeder, "white_bridge")
    ids, principal, ref = world["ids"], world["principal"], world["world_ref"]

    def transfer(tick):
        return {"from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
                "quantity": "1", "unit": "bag", "at": point(tick)}

    for tick in range(2, 6):
        ref = run_command(seeder, principal, ref, "RESOURCE_TRANSFER", transfer(tick))["world_ref"]
    worker = Kernel(kernel_env["db"], kernel_env["store"])
    with worker.db.uow(write=False) as repo:
        genesis = repo.one("genesis", {"work_id": ref["work_id"]})["payload"]
        parent = worker.revision(repo, ref)
    hits = []
    old_append = reducer._append_verified_suffix

    def independently_check(snapshot, prefix, event):
        suffix = old_append(snapshot, prefix, event)
        assert parent["state_hash"] == fingerprint(snapshot)
        assert suffix == reducer.replay(genesis, [*prefix, event])
        hits.append(event["event_version_id"])
        return suffix

    monkeypatch.setattr(reducer, "_append_verified_suffix", independently_check)
    for tick in (8, 9, 10):
        task = create_bound_task(worker, principal, ref, point(tick))
        worker.propose(principal, task["task_id"], task["generation"], "RESOURCE_TRANSFER",
                       transfer(tick), f"fixed-parent-test-{tick}")
    counts = worker.scion.counts()
    assert counts["replay_proof.prime_first"] == 1
    assert counts["replay_proof.prime_attempt"] == counts["replay_proof.primed_hit"] == 1
    assert counts["replay_proof.reuse_hit"] == 1
    with worker.db.uow(write=False) as repo:
        assert worker.head(repo, ref["work_id"]) == ref
    accepted = run_command(worker, principal, ref, "RESOURCE_TRANSFER", transfer(11))
    with worker.db.uow(write=False) as repo:
        new = worker.revision(repo, accepted["world_ref"])
        assert new["parent_revision_id"] == ref["world_revision_id"] and len(new["selection"]) == 5
    assert len(hits) == 4 and len(set(hits)) == 3
    assert worker.scion.counts()["replay_proof.reuse_hit"] == 3


def test_consistently_hashed_but_nonreplayable_parent_cannot_be_primed(kernel_env, monkeypatch):
    from copy import deepcopy

    monkeypatch.setenv("NF_SCION_REPLAY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_LAZY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_PRIME", "1")
    seed = Kernel(kernel_env["db"], kernel_env["store"])
    world = load_fixture(seed, "white_bridge")
    principal, ref, ids = world["principal"], world["world_ref"], world["ids"]
    def transfer(tick):
        return {"from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
                "quantity": "1", "unit": "bag", "at": point(tick)}
    ref = run_command(seed, principal, ref, "RESOURCE_TRANSFER", transfer(3))["world_ref"]
    worker = Kernel(kernel_env["db"], kernel_env["store"])
    tasks = [create_bound_task(worker, principal, ref, point(tick)) for tick in (4, 5, 6)]
    # The immutable DB trigger rejects modifications; simulate a corrupt row
    # returned by the database adapter (after an actual SELECT) without
    # bypassing that trigger or changing any stored authority.
    from sqlalchemy.exc import IntegrityError
    from novel_factory.infrastructure.repository import KernelRepository
    with worker.db.uow(write=False) as repo:
        before = worker.revision(repo, ref)
        forged = deepcopy(before["snapshot"])
        forged["entities"][ids["north"]]["name"] += "-fault"
    with pytest.raises(IntegrityError, match="immutable authoritative record"):
        with worker.db.uow() as repo:
            repo.change("revisions", ref, {"snapshot": forged, "state_hash": fingerprint(forged)})
    import json
    read = KernelRepository.revision_with_snapshot_text
    injected = []
    def corrupt_read(repo, world_ref):
        value = read(repo, world_ref)
        if world_ref == ref:
            value["snapshot"] = json.dumps(forged)
            value["state_hash"] = fingerprint(forged)
            injected.append(world_ref)
        return value
    monkeypatch.setattr(KernelRepository, "revision_with_snapshot_text", corrupt_read)
    for tick, task in zip((4,5,6), tasks):
        worker.propose(principal, task["task_id"], task["generation"], "RESOURCE_TRANSFER",
                       transfer(tick), f"mismatched-parent-{tick}")
    counts = worker.scion.counts()
    assert counts.get("replay.hit", 0) == 0 and counts["replay.unverified_parent"] == 3
    assert counts["replay_proof.prime_attempt"] == counts["replay_proof.prime_mismatch"] == 1
    assert len(injected) >= 3
    monkeypatch.setattr(KernelRepository, "revision_with_snapshot_text", read)
    with worker.db.uow(write=False) as repo:
        assert worker.head(repo, ref["work_id"]) == ref and worker.revision(repo, ref)["snapshot"] == before["snapshot"]
