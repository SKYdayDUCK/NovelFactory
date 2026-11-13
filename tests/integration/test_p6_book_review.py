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
        elif "归纳" in package["instruction"]:
            result = [package["observations"][0]]
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


@pytest.mark.parametrize("packet_size,expected_calls", [(6000, 4), (3, 16)])
def test_two_separate_reader_tracks_replay_and_release_gate(
    kernel_env, monkeypatch, packet_size, expected_calls
):
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
    original_packets = book_review.reading_packets
    monkeypatch.setattr(
        book_review, "reading_packets", lambda parts: original_packets(parts, limit=packet_size)
    )
    enrollment = book_review.start_review(k, gateway, owner, **params)
    assert enrollment == book_review.start_review(k, gateway, owner, **params)
    if packet_size == 6000:
        adapter.model = "unregistered-replacement"
        RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        changed = book_review.review_status(k, owner, work, branch)
        assert (
            changed["state"] == "BLOCKED"
            and changed["reason"] == "MODEL_CONFIG_CHANGED"
            and not adapter.messages
        )
        adapter.model = "blind-review-test"
        book_review.resume_review(k, owner, work, branch)
    outcomes = []
    for step in range(110):
        outcome = RuntimeSupervisor(k, gateway).run_once(owner, work, branch)
        outcomes.append((outcome.get("status"), outcome.get("reason")))
        status = book_review.review_status(k, owner, work, branch)
        assert status["state"] != "BLOCKED", (status.get("reason"), outcomes)
        if status["state"] == "ASSESSED":
            break
    assert status["state"] == "ASSESSED" and status["model_correlation"] == "SAME_MODEL", status
    assert len(adapter.messages) == expected_calls
    assert len({tid for r in status["readers"] for tid in r["tasks"]}) == expected_calls
    assert all(r["complete_packets"] == enrollment["packets"] for r in status["readers"])
    for messages in adapter.messages:
        package = json.loads(messages[1]["content"])
        assert "context" not in package and "scores" not in package and "creator" not in package
        assert "85" not in json.dumps(
            package["rubric"], ensure_ascii=False
        ) and "95" not in json.dumps(package["rubric"], ensure_ascii=False)
    assert status["readers"][0]["score_ref"] != status["readers"][1]["score_ref"]
    release = ReleaseService(k).freeze(owner, request, "reviewed-fixture")
    assert release["release_ref"] and status["result"]["average"] == "100.0"
    book = book_review.export_reviewed_book(k, owner, work, branch, release["release_ref"])
    assert book["text"] == "北窗的雨声很轻。\n\n南岸的水声很远。"
    assert [s["scene_id"] for s in book["scenes"]] == [
        e["scene_id"] for e in release["manifest"]["ordered_artifacts"]
    ]
    with k.db.uow(write=False) as repo:
        proof = k.artifact(
            repo,
            book_review._selected(
                repo, work, "P6_REVIEWED_RELEASE:" + release["release_ref"]["artifact_revision_id"]
            ),
            work,
            kind="BOOK_REVIEWED_RELEASE",
        )["payload"]
        assert (
            proof["round_ref"] == status["round_ref"]
            and proof["manifest_ref"] == release["manifest_ref"]
        )
        assert proof["score_refs"] == [r["score_ref"] for r in status["readers"]]
    full = book_review.review_status(k, owner, work, branch, include_reports=True)
    assert all(r["assessment"]["report"] and r["passage_notes"] for r in full["readers"])
    with pytest.raises(DomainError) as replay:
        book_review.start_review(
            k,
            gateway,
            owner,
            **{**params, "idempotency_key": "attempt-again", "revision_note": "same"},
        )
    assert replay.value.code == "REVIEW_ROUND_LIMIT"
