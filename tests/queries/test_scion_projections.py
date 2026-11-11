"""Same-cut reference checks for the opt-in column and token projections."""
from copy import deepcopy

import pytest
from sqlalchemy import event

from novel_factory.application.templates import build
from novel_factory.domain.errors import DomainError

from .conftest import p, seed


def test_query_projection_checks_complete_state_and_schedule(query_env, monkeypatch):
    kernel = query_env["kernel"]
    world = seed(query_env)
    wr = world["world_ref"]
    sql = []

    def capture(_conn, _cursor, statement, *_args):
        if "FROM nf_revisions" in statement:
            sql.append(statement.partition("FROM")[0])

    event.listen(kernel.db.engine, "before_cursor_execute", capture)
    try:
        with kernel.db.uow(write=False) as repo:
            old = kernel.revision(repo, wr)
            monkeypatch.setattr(kernel.scion, "query_columns", True)
            row = kernel.query_revision(repo, wr)
            assert row == {key: old[key] for key in row}
            assert "scope_versions" not in row
            assert "nf_revisions.scope_versions" in sql[0]
            assert "nf_revisions.scope_versions" not in sql[-1]
            assert "nf_revisions.snapshot" in sql[-1]

            actual_one = repo.one
            for change in ("snapshot", "provenance"):
                def corrupted(*args, field=change, **kwargs):
                    result = deepcopy(actual_one(*args, **kwargs))
                    if field == "snapshot":
                        result["snapshot"]["events"].append({})
                    else:
                        result["provenance"]["simulation_sha256"] = "0" * 64
                    return result

                monkeypatch.setattr(repo, "one", corrupted)
                for read in (kernel.query_revision, kernel.revision):
                    with pytest.raises(DomainError) as rejected:
                        read(repo, wr)
                    assert rejected.value.code == "PROJECTION_CORRUPT"
    finally:
        event.remove(kernel.db.engine, "before_cursor_execute", capture)


def test_sparse_registered_tokens_match_full_map_with_absent_range(query_env, monkeypatch):
    kernel = query_env["kernel"]
    world = seed(query_env)
    wr, ids = world["world_ref"], world["ids"]
    # The real registered template derives an empty-range dependency on blocked_by.
    with kernel.db.uow(write=False) as repo:
        revision = kernel.revision(repo, wr)
        args = {"traveler_id": ids["P"], "region_id": ids["north"],
                "destination_id": ids["south"], "at": p(3)}
        footprint = build("PASS_CLEAR_ROUTE", args, revision["snapshot"], ids["P"])
        key = "PREDICATE_RANGE:blocked_by"
        assert key in footprint["reads"]
        grant = repo.one("grants", {"grant_id": world["principal"].grant_id})
        work = repo.one("works", {"work_id": wr["work_id"]})
        parameters = repo, wr, footprint, grant, [], 0, work
        monkeypatch.setattr(kernel.scion, "scopes", False)
        expected = kernel._tokens(*parameters, template_id="PASS_CLEAR_ROUTE")
        monkeypatch.setattr(kernel.scion, "scopes", True)
        assert expected == kernel._tokens(*parameters, template_id="PASS_CLEAR_ROUTE")
        assert repo.scope_versions_for(wr["work_id"], wr["branch_id"], set()) == {}
        assert next(t for t in expected[0] if t["scope_key"] == key)["version"] == 0
    with kernel.db.uow() as repo:
        repo.bump(wr["work_id"], wr["branch_id"], {key, "ENTITY:unrelated"})
        parameters = repo, wr, footprint, grant, [], 0, work
        updated = kernel._tokens(*parameters, template_id="PASS_CLEAR_ROUTE")
        monkeypatch.setattr(kernel.scion, "scopes", False)
        assert updated == kernel._tokens(*parameters, template_id="PASS_CLEAR_ROUTE")
        assert next(t for t in updated[0] if t["scope_key"] == key)["version"] == 1
        assert all(t["scope_key"] != "ENTITY:unrelated" for group in updated for t in group)
