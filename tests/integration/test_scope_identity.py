"""Database guards prevent deletion, renaming and reset of dependency tokens."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from novel_factory.fixtures import load_fixture

pytestmark=[pytest.mark.postgres,pytest.mark.integration]

def test_scope_identity_cannot_be_renamed_or_reset_for_aba(kernel_env):
    kernel=kernel_env["kernel"];fixture=load_fixture(kernel);wr=fixture["world_ref"]
    key="ENTITY:"+fixture["ids"]["sword"]
    for change in ("scope_key=scope_key||:suffix,version=version+1","version=version"):
        with pytest.raises(DBAPIError):
            with kernel.db.engine.begin() as connection:
                connection.execute(text("UPDATE nf_scopes SET "+change+" WHERE work_id=:work AND branch_id=:branch AND scope_key=:scope"),
                                   {"work":wr["work_id"],"branch":wr["branch_id"],"scope":key,"suffix":":renamed"})
    with kernel.db.uow(write=False) as repo:
        assert repo.one("scopes",{"work_id":wr["work_id"],"branch_id":wr["branch_id"],"scope_key":key})["version"]==0
        assert repo.one("scopes",{"work_id":wr["work_id"],"branch_id":wr["branch_id"],"scope_key":key+":renamed"}) is None


def test_overlapping_scope_batches_increment_once_per_transaction_and_roll_back(kernel_env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr = world["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    existing = "ENTITY:" + world["ids"]["sword"]
    keys = [existing, "TEST:alpha", "TEST:文字", "TEST:é", existing]
    with kernel.db.uow() as repo:
        before = repo.bump(work, branch, [])

    entered = Barrier(2)

    def apply(batch):
        with kernel.db.uow() as repo:
            entered.wait(timeout=10)
            repo.bump(work, branch, batch)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(apply, batch) for batch in (keys, list(reversed(keys)))]
        for future in futures:
            future.result(timeout=20)

    expected = dict(before)
    for key in set(keys):
        expected[key] = before.get(key, 0) + 2
    with pytest.raises(RuntimeError, match="roll back"):
        with kernel.db.uow() as repo:
            assert repo.bump(work, branch, []) == expected
            bumped = repo.bump(work, branch, [*keys, "TEST:rollback"])
            assert bumped[existing] == expected[existing] + 1
            raise RuntimeError("roll back")
    with kernel.db.uow(write=False) as repo:
        assert repo.scope_map(work, branch) == expected
