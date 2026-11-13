"""Transport simulation of blind assignment, kept separate from production evidence."""

import json
from decimal import Decimal
import pytest

from novel_factory.application import book_review, production_extent
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.production_profiles import bind_profile, resolve_profile
from novel_factory.application.release import ReleaseService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.book_review import RUBRIC
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.model_gateway import ModelResponse, OpenAICompatibleAdapter
from test_release import complete_candidate, assert_no_release

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class ReadingTransport(OpenAICompatibleAdapter):
    def __init__(self):
        super().__init__(
            endpoint="https://reader.invalid/v1/chat/completions",
            model="blind-review-test",
            api_key="test-only",
            max_output_tokens=4096,
        )
        self.messages = []

    def generate(self, request):
        self.messages.append(request.messages)
        system, user = request.messages
        identity = json.loads(
            system["content"].split("\nIdentity: ")[1].split("\nPayload JSON Schema: ")[0]
        )
        package = json.loads(user["content"])
        if "passages" in package:
            result = [
                dict(
                    scene=p["scene"], start=p["start"], quote=p["text"][:2], observation="测试观察"
                )
                for p in package["passages"]
            ]
        else:
            citation = {k: package["observations"][0][k] for k in ("scene", "start", "quote")}
            result = {
                "scores": {
                    k: {**citation, "score": v, "reason": "测试判断"} for k, v in RUBRIC.items()
                },
                "vetoes": [],
            }
        payload = dict(
            summary=json.dumps(result, ensure_ascii=False),
            issues=[],
            evidence_refs=[],
            counterinterpretations=[],
        )
        envelope = {
            **identity,
            "status": "SUCCEEDED",
            "unresolved_questions": [],
            "payload": payload,
        }
        return ModelResponse(
            text=json.dumps(envelope, ensure_ascii=False),
            provider_receipt="TEST_FIXTURE:" + request.request_sha256,
            request_sha256=request.request_sha256,
            input_tokens=20,
            output_tokens=40,
            cached_input_tokens=0,
            actual_amount=Decimal(0),
            currency="USD",
            raw_response={"test": True},
        )


def test_two_separate_reader_tracks_replay_and_release_gate(kernel_env, monkeypatch):
    k = kernel_env["kernel"]
    f, accepted, binding, proof, request = complete_candidate(k)
    owner, wr = f["principal"], f["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    with k.db.uow() as repo:
        bind_profile(k, repo, owner, wr, resolve_profile("short", "测试"))
    adapter = ReadingTransport()
    gateway = AgentGateway(k, {"one": adapter, "two": adapter})
    params = dict(
        release_request=request,
        routes=["one", "two"],
        budget_id=k.tasks.create_budget(work, "3"),
        reserve_amount="0.1",
        token_reservation=150000,
        deadline_seconds=1200,
        idempotency_key="test-reading",
    )
    with pytest.raises(DomainError) as no_read:
        ReleaseService(k).freeze(owner, request, "unreviewed")
    assert no_read.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    with pytest.raises(DomainError) as simulated:
        book_review.start_review(k, gateway, owner, **params)
    assert simulated.value.code == "MODEL_UNAVAILABLE"
    assert_no_release(k, f)
    # Isolate the reader workflow under test. These two test substitutions are never used in production.
    monkeypatch.setattr(book_review, "is_fixture_configuration", lambda config: False)
    monkeypatch.setattr(
        production_extent, "measure_submission", lambda *args: {"source": "TEST_FIXTURE"}
    )
    enrollment = book_review.start_review(k, gateway, owner, **params)
    assert enrollment == book_review.start_review(k, gateway, owner, **params)
    outcomes = []
    for step in range(25):
        outcome = RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        outcomes.append((outcome.get("status"), outcome.get("reason")))
        status = book_review.review_status(k, owner, work, branch)
        assert status["state"] != "BLOCKED", (status.get("reason"), outcomes)
        if status["state"] == "ASSESSED":
            break
    assert status["state"] == "ASSESSED", status
    assert len(adapter.messages) == 4
    assert len({tid for r in status["readers"] for tid in r["tasks"]}) == 4
    assert all(r["complete_packets"] == 1 for r in status["readers"])
    for messages in adapter.messages:
        package = json.loads(messages[1]["content"])
        assert "context" not in package and "scores" not in package and "creator" not in package
        assert "85" not in json.dumps(
            package["rubric"], ensure_ascii=False
        ) and "95" not in json.dumps(package["rubric"], ensure_ascii=False)
    assert status["readers"][0]["score_ref"] != status["readers"][1]["score_ref"]
    release = ReleaseService(k).freeze(owner, request, "reviewed-fixture")
    assert release["release_ref"] and status["result"]["average"] == "100.0"
    with pytest.raises(DomainError) as replay:
        book_review.start_review(
            k,
            gateway,
            owner,
            **{**params, "idempotency_key": "attempt-again", "revision_note": "same"},
        )
    assert replay.value.code == "REVIEW_ROUND_LIMIT"
