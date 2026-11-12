import os
from uuid import uuid4

import pytest
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.organization import bootstrap
from agent_workshop.server import dispatch


def test_manager_only_versioned_publish_and_export():
    dsn = os.environ["AW_DATABASE_URL"]
    org, inst, work, branch = (str(uuid4()) for _ in range(4))
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,'nf',true)", (inst,))
        bootstrap(conn, org, installation_id=inst)
        admin = {"database_url": dsn, "token": enroll(conn, inst, org, "manager", 1, 1)}
        actor = {"database_url": dsn, "token": enroll(conn, inst, org, "actor-1", 1, 1)}
    req = {"work_id": work, "branch_id": branch}
    rules = [{"id": "system", "when": {}, "profile": "codex-v1"}]
    args = {**req, "expected_revision": 0, "rules": rules}
    before = dispatch(admin, "aw_model_policy_publish", args)
    assert dispatch(admin, "aw_model_policy_publish", args) == before
    assert dispatch(admin, "aw_model_policy_get", req) == before
    assert not {"aw_model_policy_get", "aw_model_policy_publish"}.intersection(dispatch(actor))
    with pytest.raises(Denied):
        dispatch(actor, "aw_model_policy_get", req)
    with pytest.raises(Denied, match="STALE_REVISION"):
        dispatch(admin, "aw_model_policy_publish", {**args, "expected_revision": 9})
    with pytest.raises(Denied, match="INVALID_ARGUMENT"):
        dispatch(admin, "aw_model_policy_publish", {**args, "rules": [{**rules[0], "api_key": "fixture"}]})
