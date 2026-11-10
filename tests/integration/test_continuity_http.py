"""Cross-volume HTTP lifecycle binds independent review and retains kernel auth."""
import pytest
from fastapi.testclient import TestClient
from test_continuity import request, uid, volume

from novel_factory.fixtures import point
from novel_factory.interfaces.http import create_app

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_http_continuity_review_publish_inspect_and_character_denial(kernel_env):
    k = kernel_env["kernel"]
    f, frozen, _ = volume(k)
    wid, p = f["world_ref"]["work_id"], f["principal"]
    review = k.issue_grant(p, wid, role_id="volume_director",
        capabilities=["READ", "ARTIFACT_WRITE", "APPROVE", "PLAN_READ"],
        planes=["WORLD", "PLAN"], max_story_time=point(1000))
    actor = k.issue_grant(p, wid, role_id="character_actor", actor_id=f["ids"]["protagonist"],
        capabilities=["READ"], planes=["BELIEF"], max_story_time=point(1000))
    admin_headers = {"Authorization": "Bearer " + f["token"]}
    review_headers = {"Authorization": "Bearer " + review["token"]}
    base = f"/api/v1/works/{wid}/continuity/"
    body = request(f, [frozen])
    with TestClient(create_app(k)) as client:
        assert client.post("/api/v1/continuity/prepare", json=body).status_code == 401
        prepared = client.post("/api/v1/continuity/prepare", json=body, headers=admin_headers)
        assert prepared.status_code == 200, prepared.json()
        candidate = prepared.json()
        reading = client.post(base + "readings", json={"schema_version": "2.3",
            "candidate_ref": candidate, "idempotency_key": uid()}, headers=review_headers)
        assert reading.status_code == 200, reading.json()
        reviewed = client.post(base + "reviews", json={"schema_version": "2.3",
            "candidate_ref": candidate, "reading_ref": reading.json()["reading_ref"],
            "decision": "APPROVED", "reason": "Independent identity, deterministic transport fixture",
            "acknowledged_affected_ordinals": [], "idempotency_key": uid()}, headers=review_headers)
        assert reviewed.status_code == 200, reviewed.json()
        pub = {"schema_version": "2.3", "candidate_ref": candidate,
               "review_ref": reviewed.json(), "idempotency_key": uid()}
        result = client.post(base + "publish", json=pub, headers=admin_headers)
        assert result.status_code == 200, result.json()
        assert client.post(base + "publish", json=pub, headers=admin_headers).json() == result.json()
        inspect = {"schema_version": "2.3", "snapshot_ref": result.json()["snapshot_ref"]}
        assert client.post(base + "inspect", json=inspect, headers=admin_headers).json()["qualification"] == "NO_NEW_IMPACT_SINCE_REVIEW"
        denied = client.post(base + "inspect", json=inspect,
                             headers={"Authorization": "Bearer " + actor["token"]})
        assert denied.status_code == 403 and denied.json()["code"] == "VIEW_DENIED"
        bad = client.post(base + "inspect", json={**inspect, "unsafe_bypass": True}, headers=admin_headers)
        assert bad.status_code == 422
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wid) == f["world_ref"]
        assert not repo.many("events", {"work_id": wid})
