"""The Agent-facing transport uses the same finite proof and auth boundary."""
from fastapi.testclient import TestClient
import pytest

from novel_factory.fixtures import point
from novel_factory.interfaces.http import create_app
from tests.integration.test_refinement import accepted_transfer, request_for

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_http_refinement_is_idempotent_and_does_not_accept_world_effects(service_env):
    kernel = service_env["kernel"]
    world = accepted_transfer(kernel)
    request, _ = request_for(kernel, world)
    with TestClient(create_app(kernel)) as client:
        missing = client.post("/api/v1/refinements/prepare", json=request)
        assert missing.status_code == 401
        headers = {"Authorization": "Bearer " + world["token"]}
        first = client.post("/api/v1/refinements/prepare", json=request, headers=headers)
        assert first.status_code == 200, first.json()
        again = client.post("/api/v1/refinements/prepare", json=request, headers=headers)
        assert again.json() == first.json()
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, world["world_ref"]["work_id"]) == world["world_ref"]
        assert len(kernel.revision(repo, world["world_ref"])["selection"]) == 1


def test_http_refinement_denies_character_view(service_env):
    kernel = service_env["kernel"]
    world = accepted_transfer(kernel)
    request, _ = request_for(kernel, world)
    actor = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="character_actor", actor_id=world["ids"]["protagonist"],
        capabilities=["READ"], planes=["BELIEF"], max_story_time=point(40))
    with TestClient(create_app(kernel)) as client:
        result = client.post("/api/v1/refinements/prepare", json=request,
                             headers={"Authorization": "Bearer " + actor["token"]})
        assert result.status_code == 403
        assert result.json()["code"] == "VIEW_DENIED"
