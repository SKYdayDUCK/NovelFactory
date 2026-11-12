"""The hot scalar selector caches its SQL shape and reads each value from PostgreSQL."""
import pytest

from .conftest import seed

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_repository_scalar_selects_keep_values_locks_and_membership_distinct(query_env):
    kernel = query_env["kernel"]
    a, b = seed(query_env), seed(query_env)
    with kernel.db.uow() as repo:
        for lock in (None, "share", "update"):
            for work in (a["world_ref"], b["world_ref"], a["world_ref"]):
                by_value = {"work_id": work["work_id"], "branch_id": work["branch_id"]}
                first = repo.one("branches", by_value, lock=lock)
                second = repo.many("branches", by_value, lock=lock, limit=1)[0]
                assert first == second and first["head_revision_id"] == work["world_revision_id"]
        assert repo.one("branches", {"branch_id": [a["world_ref"]["branch_id"]]})["work_id"] == a["world_ref"]["work_id"]
        assert repo.one("branches", {"branch_id": None}) is None
