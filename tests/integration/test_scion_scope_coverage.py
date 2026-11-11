"""Sparse token lookup covers missing keys, isolation and both authority boundaries."""
import pytest

from novel_factory.fixtures import load_fixture, point, prepare_command
from novel_factory.infrastructure.repository import KernelRepository


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_scope_lookup_stays_on_work_branch_and_defaults_missing_to_zero(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel, "white_bridge")
    principal, wr, ids = world["principal"], world["world_ref"], world["ids"]
    sibling = kernel.fork(principal, wr, "REPAIR", "sparse-sibling")
    unrelated = load_fixture(kernel, "r1_sword")["world_ref"]
    footprint = {"reads": ["ABSENCE_RANGE:a", "PREDICATE_RANGE:occupied_by", "ENTITY:" + ids["warehouse"]],
                 "writes": ["ABSENCE_RANGE:a"]}
    # Deliberately advance a negative/range key only on the sibling branch.
    with kernel.db.uow() as repo:
        repo.bump(wr["work_id"], sibling["branch_id"], ["ABSENCE_RANGE:a", "PREDICATE_RANGE:occupied_by"])
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        grant = repo.one("grants", {"grant_id": principal.grant_id})
        work = repo.one("works", {"work_id": wr["work_id"]})
        for selected, expected in ((wr, 0), (sibling, 1)):
            branch = repo.one("branches", {"work_id": wr["work_id"], "branch_id": selected["branch_id"]})
            def tokens(enabled):
                previous = kernel.scion.scopes
                try:
                    kernel.scion.scopes = enabled
                    return kernel._tokens(repo, selected, footprint, grant, [], branch["history_epoch"], work,
                                          template_id="PASS_CLEAR_ROUTE")
                finally:
                    kernel.scion.scopes = previous
            reference = tokens(False)
            assert tokens(True) == reference
            assert reference[0][0]["version"] == reference[1][0]["version"] == expected
        assert repo.scope_versions_for(unrelated["work_id"], sibling["branch_id"], footprint["reads"]) == {}
        assert repo.scope_versions_for(wr["work_id"], wr["branch_id"], []) == {}


def test_prepare_accept_and_idempotent_retry_use_only_required_scope_reads(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel, "white_bridge")
    ids, wr = world["ids"], world["world_ref"]
    monkeypatch.setattr(kernel.scion, "scopes", True)
    seen = []
    method = KernelRepository.scope_versions_for

    def read(repo, work_id, branch_id, keys):
        seen.append((work_id, branch_id, frozenset(keys)))
        return method(repo, work_id, branch_id, keys)

    monkeypatch.setattr(KernelRepository, "scope_versions_for", read)
    command = prepare_command(kernel, world["principal"], wr, "RESOURCE_TRANSFER", {
        "from_account": ids["warehouse"], "to_account": ids["relief"], "resource_id": ids["grain"],
        "quantity": "1", "unit": "bag", "at": point(10)}, mode="DEPENDENCY_GUARDED")
    assert len(seen) == 1
    receipt = kernel.accept(world["principal"], command["request"])
    assert len(seen) == 2 and seen[0] == seen[1]
    with kernel.db.uow(write=False) as repo:
        proposal = kernel._proposal(repo, world["principal"], command["request"])
        footprint = proposal["footprint"]
        expected_keys = frozenset(footprint["reads"] + footprint["writes"])
        assert seen[0] == (wr["work_id"], wr["branch_id"], expected_keys)
        committed = kernel.revision(repo, receipt["world_ref"])
        assert committed["scope_versions"] == repo.scope_map(wr["work_id"], wr["branch_id"])
    assert kernel.accept(world["principal"], command["request"]) == receipt
    assert len(seen) == 2


def test_strict_only_relation_uses_full_scope_map_at_both_authority_boundaries(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel, "r1_sword")
    ids = world["ids"]
    monkeypatch.setattr(kernel.scion, "scopes", True)
    before = kernel.scion.counts().get("scopes.full_template", 0)
    def unexpected(*args, **kwargs):
        pytest.fail("A template without a closed guarded footprint used sparse lookup")
    monkeypatch.setattr(KernelRepository, "scope_versions_for", unexpected)
    command = prepare_command(kernel, world["principal"], world["world_ref"], "SET_RELATION", {
        "subject_id": ids["sword"], "predicate": "legal_owner",
        "object_value": {"kind": "ENTITY", "entity_id": ids["friend"]}, "at": point(10)})
    kernel.accept(world["principal"], command["request"])
    assert kernel.scion.counts()["scopes.full_template"] == before + 2
