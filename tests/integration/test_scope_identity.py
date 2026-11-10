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
