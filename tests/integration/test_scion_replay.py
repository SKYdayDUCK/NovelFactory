"""PostgreSQL differential replay, including JSONB selection order and rollback."""
from copy import deepcopy

import pytest
from sqlalchemy import text

from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.world import replay
from novel_factory.fixtures import entity_value, load_fixture, point, prepare_command, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _transfer(ids, tick):
    return {"from_account": ids["warehouse"], "to_account": ids["relief"],
            "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(tick)}


@pytest.mark.parametrize("lazy", ["0", "1"], ids=["original", "lazy-admission"])
def test_proved_suffix_matches_selected_payloads_and_survives_rollback(kernel_env, monkeypatch, lazy):
    monkeypatch.setenv("NF_SCION_REPLAY_LAZY", lazy)
    monkeypatch.setenv("NF_SCION_REPLAY", "1")
    kernel = Kernel(kernel_env["db"], kernel_env["store"])
    with kernel.db.uow(write=False) as repo:
        assert int(repo.conn.scalar(text("SHOW server_version_num"))) // 10000 == 17
    fixture = load_fixture(kernel, "white_bridge")
    principal, wr, ids = fixture["principal"], fixture["world_ref"], fixture["ids"]
    captured = []
    from novel_factory.domain import world
    append = world._append_verified_suffix

    def observe(parent, prefix, event):
        captured.append(tuple(e["event_version_id"] for e in prefix))
        return append(parent, prefix, event)

    monkeypatch.setattr(world, "_append_verified_suffix", observe)
    for tick in range(2, 7):
        base = wr
        wr = run_command(kernel, principal, base, "RESOURCE_TRANSFER", _transfer(ids, tick))["world_ref"]
        with kernel.db.uow(write=False) as repo:
            before = kernel.revision(repo, base)
            after = kernel.revision(repo, wr)
            assert after["parent_revision_id"] == base["world_revision_id"]
            genesis = repo.one("genesis", {"work_id": wr["work_id"]})["payload"]
            rows = repo.many("events", {"work_id": wr["work_id"]})
            payloads = {r["event_version_id"]: r["payload"] for r in rows}
            new = set(after["selection"].values()) - set(before["selection"].values())
            assert len(new) == 1
            sequence = [*before["selection"].values(), new.pop()]
            expected = replay(genesis, [payloads[eid] for eid in sequence])
            assert after["snapshot"] == expected
            assert after["state_hash"] == fingerprint(expected)
    assert [len(ids) for ids in captured] == [1, 1, 2, 2, 3, 3, 4, 4]

    prepared = prepare_command(kernel, principal, wr, "RESOURCE_TRANSFER", _transfer(ids, 7))

    def fail(stage):
        if stage == "before_commit":
            raise RuntimeError("trial rollback")

    with pytest.raises(RuntimeError, match="trial rollback"):
        kernel.accept(principal, prepared["request"], fault=fail)
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, wr["work_id"]) == wr
    receipt = kernel.accept(principal, prepared["request"])
    assert kernel.accept(principal, deepcopy(prepared["request"])) == receipt


@pytest.mark.parametrize("earlier", [False, True], ids=["same-time", "out-of-order"])
def test_fallback_seeds_next_later_parent_and_process_restart_stays_exact(kernel_env, monkeypatch, earlier):
    monkeypatch.setenv("NF_SCION_REPLAY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_LAZY", "1")
    kernel = Kernel(kernel_env["db"], kernel_env["store"])
    world = load_fixture(kernel, "white_bridge")
    ids, principal, ref = world["ids"], world["principal"], world["world_ref"]
    ref = run_command(kernel, principal, ref, "RESOURCE_TRANSFER", _transfer(ids, 6))["world_ref"]
    unrelated = {"subject_id": ids["sword2"], "predicate": "legal_owner",
                 "object_value": entity_value(ids["smith2"]), "at": point(4 if earlier else 6)}
    ref = run_command(kernel, principal, ref, "SET_RELATION", unrelated)["world_ref"]
    assert kernel.scion.counts()["replay.not_strictly_later"] == 2
    assert kernel.scion.counts()["replay_proof.full_replay_seed"] == 1
    for index, worker in enumerate((kernel, Kernel(kernel_env["db"], kernel_env["store"]))):
        before = ref
        ref = run_command(worker, principal, before, "RESOURCE_TRANSFER", _transfer(ids, 7 + index))["world_ref"]
        counts = worker.scion.counts()
        assert counts.get("replay.hit", 0) == (2 if index == 0 else 0)
        if index == 1:
            assert counts["replay_proof.cold_proposal_skipped"] == 1
        with worker.db.uow(write=False) as repo:
            parent = worker.revision(repo, before)
            current = worker.revision(repo, ref)
            bodies = {row["event_version_id"]: row["payload"] for row in repo.many("events", {"work_id": ref["work_id"]})}
            new, = set(current["selection"].values()) - set(parent["selection"].values())
            ordered = [bodies[version] for version in (*parent["selection"].values(), new)]
            genesis = repo.one("genesis", {"work_id": ref["work_id"]})["payload"]
            oracle = replay(genesis, ordered)
            assert current["snapshot"] == oracle and current["state_hash"] == fingerprint(oracle)


def test_evicted_historical_parent_can_still_validate_a_pinned_proposal(kernel_env, monkeypatch):
    from novel_factory.application.replay_proofs import ReplayProofs
    from novel_factory.fixtures import create_bound_task

    monkeypatch.setenv("NF_SCION_REPLAY", "1")
    monkeypatch.setenv("NF_SCION_REPLAY_LAZY", "1")
    kernel = Kernel(kernel_env["db"], kernel_env["store"])
    kernel.replay_proofs = ReplayProofs(capacity=1)
    world = load_fixture(kernel, "white_bridge")
    ids, principal, base = world["ids"], world["principal"], world["world_ref"]
    pinned = run_command(kernel, principal, base, "RESOURCE_TRANSFER", _transfer(ids, 3))["world_ref"]
    head = run_command(kernel, principal, pinned, "RESOURCE_TRANSFER", _transfer(ids, 4))["world_ref"]
    task = create_bound_task(kernel, principal, pinned, point(8))
    before = kernel.scion.counts()
    proposal = kernel.propose(principal, task["task_id"], task["generation"], "RESOURCE_TRANSFER",
                              _transfer(ids, 8), "historical-pinned-after-eviction")
    after = kernel.scion.counts()
    assert proposal["proposal_ref"] and after["replay.hit"] == before["replay.hit"]
    assert after["replay.unverified_parent"] == before["replay.unverified_parent"] + 1
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, base["work_id"]) == head
