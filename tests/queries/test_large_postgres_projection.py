"""Live large-world reads check bytes on every access, including concurrent scopes."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from benchmarks.p2_behavioral_load import make_population
from novel_factory.domain.errors import DomainError
from .conftest import seed

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_large_live_projection_fences_same_size_corruption_across_concurrent_works(query_env):
    kernel = query_env["kernel"]
    large = make_population(kernel, 500, .8)["world"]["world_ref"]
    independent = seed(query_env)["world_ref"]

    def read(ref):
        with kernel.db.uow(write=False) as repo:
            result = kernel.revision(repo, ref)
        assert {k: result[k] for k in ref} == ref
        return result

    first = read(large)
    digest = first["state_hash"]
    ent, entity = next((i, e) for i, e in first["snapshot"]["entities"].items()
                       if e.get("name", "").startswith("TEST_FIXTURE_"))
    name = entity["name"]
    first["snapshot"]["entities"].clear()
    assert read(large)["snapshot"]["entities"][ent]["name"] == name
    separate_digest = read(independent)["state_hash"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        mixed = list(pool.map(read, [large, independent] * 4))
    assert [row["state_hash"] for row in mixed] == [digest, separate_digest] * 4

    # Emulate damaged restored storage. The owner changes one byte and leaves
    # the advertised canonical hash, revision ID and serialized length intact.
    read(large)
    with kernel.db.uow() as repo:
        repo.conn.execute(text("SET LOCAL session_replication_role = replica"))
        size_sql = text("SELECT octet_length(snapshot::text) FROM nf_revisions WHERE world_revision_id=:id")
        where = {"id": large["world_revision_id"]}
        before = repo.conn.execute(size_sql, where).scalar_one()
        assert before > 1_000_000
        repo.conn.execute(text("""
            UPDATE nf_revisions SET snapshot=jsonb_set(snapshot,
                ARRAY['entities',cast(:entity as text),'name'],to_jsonb(cast(:name as text)))
            WHERE world_revision_id=:id
        """), {**where, "entity": ent, "name": "Z" + name[1:]})
        assert repo.conn.execute(size_sql, where).scalar_one() == before

    def status(ref):
        try:
            return read(ref)["state_hash"]
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(status, [large, independent] * 4))
    assert checked == ["PROJECTION_CORRUPT", separate_digest] * 4
