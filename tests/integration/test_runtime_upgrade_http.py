"""Transport carries reviewed upgrades and server-configured model choices only."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_runtime_upgrades import upgrade_request

from novel_factory.fixtures import load_fixture, point
from novel_factory.interfaces.http import create_app

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_http_runtime_review_activation_receipt_and_server_only_model_route(kernel_env):
    k = kernel_env["kernel"]
    world = load_fixture(k)
    headers = {"Authorization": "Bearer " + world["token"]}
    reviewer = k.issue_grant(world["principal"], world["world_ref"]["work_id"],
        role_id="reviewer", capabilities=["READ", "APPROVE"], planes=["WORLD"], max_story_time=point(100))
    adapters = {"fixture-v1": SimpleNamespace(configuration={"execution_kind": "TEST_FIXTURE", "model": "fixture"})}
    with TestClient(create_app(k, model_adapters=adapters)) as client:
        request = upgrade_request(k, world)
        assert client.post("/api/v1/runtime-upgrades/prepare", json=request).status_code == 401
        prepared = client.post("/api/v1/runtime-upgrades/prepare", json=request, headers=headers)
        assert prepared.status_code == 200, prepared.json()
        review = {"schema_version": "2.1", "world_ref": world["world_ref"],
            "package_ref": prepared.json()["package_ref"], "decision": "APPROVE",
            "notes": "HTTP fixture, separately authenticated reviewer", "idempotency_key": "http-review"}
        own = client.post("/api/v1/runtime-upgrades/reviews", json=review, headers=headers)
        assert own.status_code == 409 and own.json()["code"] == "INDEPENDENT_REVIEW_REQUIRED"
        approved = client.post("/api/v1/runtime-upgrades/reviews", json=review,
                              headers={"Authorization": "Bearer " + reviewer["token"]})
        assert approved.status_code == 200, approved.json()
        activation = {"schema_version": "2.1", "world_ref": world["world_ref"],
            "package_ref": review["package_ref"], "approval_ref": approved.json(), "idempotency_key": "http-activate"}
        activated = client.post("/api/v1/runtime-upgrades/activate", json=activation, headers=headers)
        assert activated.status_code == 200, activated.json()
        assert client.post("/api/v1/runtime-upgrades/activate", json=activation, headers=headers).json() == activated.json()
        route = {"schema_version": "2.1", "world_ref": activated.json()["world_ref"],
                 "adapter_id": "fixture-v1", "expected_ref": None, "idempotency_key": "http-model"}
        selected = client.post("/api/v1/model-routes/select", json=route, headers=headers)
        assert selected.status_code == 200, selected.json()
        assert client.post("/api/v1/model-routes/select", json=route, headers=headers).json() == selected.json()
        malicious = client.post("/api/v1/model-routes/select", json={**route, "provider_url": "https://unconfigured.invalid"}, headers=headers)
        assert malicious.status_code == 422
    with k.db.uow(write=False) as repo:
        assert not repo.many("events", {"work_id": world["world_ref"]["work_id"]})
        assert k.head(repo, world["world_ref"]["work_id"]) == activated.json()["world_ref"]
