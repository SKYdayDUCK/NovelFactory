"""Exercise task freeze, work isolation and the actual provider prompt with PostgreSQL."""
from uuid import uuid4

import pytest

from novel_factory.application.knowledge_delivery import bound_methods
from novel_factory.application.production_profiles import bind_profile, resolve_profile
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from test_agent_gateway import setup_gateway

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_p6_actor_freezes_only_scoped_public_methods_for_dispatch(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        bind_profile(kernel, repo, owner, wr, resolve_profile("short", "灵异"))
    case = setup_gateway(kernel_env, role="character_actor", world=world)
    with kernel.db.uow(write=False) as repo:
        data = kernel.artifact(repo, case["request"]["input_ref"], wr["work_id"])["payload"]
        ref = data["knowledge_profile_ref"]
        notes = kernel.artifact(repo, ref, wr["work_id"], kind="P6_KNOWLEDGE_PROFILE")["payload"]["methods"]
        assert notes["genre"] == "supernatural" and notes["entries"]
        assert all("original" not in entry for entry in notes["entries"])
        other_branch = {**case["spec"], "world_ref": {**wr, "branch_id": str(uuid4())}}
        for other in (other_branch, {**case["spec"], "role_id": "reviewer"}):
            with pytest.raises(DomainError, match="outside this task binding"):
                bound_methods(kernel, repo, other, ref)

    def cannot_reselect(*args, **kwargs):
        pytest.fail("dispatch attempted to select from the current knowledge catalog")

    monkeypatch.setattr("novel_factory.application.knowledge_delivery.freeze_knowledge_profile", cannot_reselect)
    result = case["gateway"].execute(case["principal"], case["request"])
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    system = case["adapter"].calls[0].messages[0]["content"]
    assert all(entry["method"] in system and entry["limit"] in system for entry in notes["entries"])
    assert "original_ref" not in system
