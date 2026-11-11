"""PostgreSQL differential replay, including JSONB selection order and rollback."""
from copy import deepcopy

import pytest
from sqlalchemy import text

from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.world import replay
from novel_factory.fixtures import load_fixture, point, prepare_command, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _transfer(ids, tick):
    return {"from_account": ids["warehouse"], "to_account": ids["relief"],
            "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(tick)}


def test_proved_suffix_matches_selected_payloads_and_survives_rollback(kernel_env, monkeypatch):
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
