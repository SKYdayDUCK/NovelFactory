"""An actual 0006 resume grant cannot advance ready work after its fence changes."""
from datetime import timedelta

import pytest

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.domain.errors import DomainError
from novel_factory.orchestration.workflow import WorkflowService
from .conftest import start_task
from .test_workflow import accept, blocked, scope_args

pytestmark = [pytest.mark.postgres]


@pytest.mark.parametrize("invalidate", ["expiry", "policy", "revoke"])
def test_expired_project_credential_does_not_consume_dependency(rel_env, rel_scope, invalidate):
    kernel = Kernel(rel_env["db"], rel_env["store"])
    opts = scope_args(rel_scope)
    with kernel.db.uow() as repo:
        repo.change("grants", {"grant_id": rel_scope["grant_id"]}, {"capabilities": ["ADMIN"]})
        expiry = repo.now() + timedelta(minutes=10)
    issuer = Principal(rel_scope["grant_id"], rel_scope["principal_id"])
    grant = kernel.issue_grant(issuer, opts["work_id"], branch_ids=[opts["branch_id"]],
        role_id="resume_operator", capabilities=["PROJECT_RESUME"], expires_at=expiry)
    runtime = McpRuntime(kernel, grant["principal"], ProjectBinding(opts["work_id"], opts["branch_id"]))
    pred, target = start_task(rel_env, rel_scope), blocked(rel_env, rel_scope)
    WorkflowService(kernel.tasks).wait(target, 0, [pred["task_id"]], mode="AND", **opts)
    accept(rel_env, pred)
    assert runtime.project()["counts"]["WAITING"] == 1
    with kernel.db.uow() as repo:
        grant_id = {"grant_id": grant["principal"].grant_id}
        if invalidate == "expiry":
            repo.change("resume_grants", grant_id, {"expires_at": repo.now()})
        elif invalidate == "policy":
            repo.change("resume_grants", grant_id, {"policy_version": "obsolete-policy"})
        else:
            repo.change("grants", grant_id, {"active": False})
    with pytest.raises(DomainError) as denied:
        runtime.project(resume=True)
    assert denied.value.code == "VIEW_DENIED"
    assert kernel.tasks.get(target)["state"] == "BLOCKED"
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("inbox", {"consumer": f"workflow:{target}:0"})
