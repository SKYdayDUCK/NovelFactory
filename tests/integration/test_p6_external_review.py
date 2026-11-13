"""Fixture manuscripts and ephemeral signing keys exercise the host handoff on real PostgreSQL."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_release import complete_candidate

from novel_factory.application import book_review, external_book_review, production_extent
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.production_profiles import bind_profile, resolve_profile
from novel_factory.application.release import ReleaseService
from novel_factory.domain.book_review import PROTOCOL, PROTOCOL_HASH, RUBRIC
from novel_factory.domain.errors import DomainError
from scripts.p6_external_review import main

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def private_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def assessment(package, *, veto=False):
    notes = [
        [
            {
                "scene": p["scene"],
                "start": p["start"],
                "quote": p["text"][:2],
                "observation": "夹具观察",
            }
            for p in packet["passages"]
        ]
        for packet in package["packets"]
    ]
    cite = {key: notes[0][0][key] for key in ("scene", "start", "quote")}
    report = {
        "scores": {k: {**cite, "score": v, "reason": "测试举证"} for k, v in RUBRIC.items()},
        "vetoes": [{"kind": PROTOCOL["vetoes"][0], **cite, "reason": "夹具否决"}] if veto else [],
    }
    return {"observations": notes, "report": report}


@pytest.mark.parametrize("outcome", ["pass", "veto", "missing_passage"])
def test_authenticated_blind_handoff(kernel_env, tmp_path, monkeypatch, outcome):
    k = kernel_env["kernel"]
    fixture, _, _, _, request = complete_candidate(k)
    owner, wr = fixture["principal"], fixture["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    scope = (k, owner, work, branch)
    with k.db.uow() as repo:
        bind_profile(k, repo, owner, wr, resolve_profile("short", "测试"))
    # Fixtures have real reviewed, accepted drafts but no billable native writer. Do not certify these as books.
    monkeypatch.setattr(
        production_extent, "measure_submission", lambda *args: {"source": "TEST_FIXTURE"}
    )
    split = book_review.reading_packets
    monkeypatch.setattr(book_review, "reading_packets", lambda parts: split(parts, limit=5))
    keys = [str(tmp_path / f"{n}.key") for n in (1, 2)]
    judges = [
        {"session_id": f"test-host/session-{n}", **main(["keygen", "--key", key])}
        for n, key in enumerate(keys, 1)
    ]
    settings = {
        "release_request": request,
        "external_judges": judges,
        "deadline_seconds": 1200,
        "idempotency_key": "test-host-registration",
    }
    gateway = AgentGateway(k, {})
    for field in ("session_id", "public_key"):
        bad = deepcopy(settings)
        bad["external_judges"][1][field] = judges[0][field]
        with pytest.raises(DomainError, match="distinct"):
            book_review.start_review(k, gateway, owner, **bad)
    invalid = deepcopy(settings)
    invalid["external_judges"][0]["public_key"] = "00" * 33
    with pytest.raises(DomainError) as bad_key:
        book_review.start_review(k, gateway, owner, **invalid)
    assert bad_key.value.code == "INVALID_ARGUMENT"
    binding = private_json(
        tmp_path / "binding.json",
        {
            "database_url": kernel_env["url"],
            "object_root": str(k.objects.root),
            "token": fixture["token"],
            "binding": {"work_id": work, "branch_id": branch},
        },
    )
    enroll = [
        "enroll",
        "--binding",
        binding,
        "--settings",
        private_json(tmp_path / "start.json", settings),
    ]
    registration = main(enroll)
    assert registration == main(enroll)
    assert book_review.advance_review(k, gateway, owner, work, branch) is None
    if outcome == "pass":
        # The CLI can recover the approved initial release request from the bound rolling project.
        from novel_factory.application import rolling_publication

        monkeypatch.setattr(rolling_publication, "final_review_request", lambda *args: request)
        implicit = {key: value for key, value in settings.items() if key != "release_request"}
        private_json(tmp_path / "start.json", implicit)
        assert registration == main(enroll)
    outsider = k.issue_grant(owner, work, role_id="writer", capabilities=["READ"])["principal"]
    with pytest.raises(DomainError) as unauthorized:
        external_book_review.export_assignment(k, outsider, work, branch, track=1)
    assert unauthorized.value.code == "VIEW_DENIED"
    payloads, envelopes, files = [], [], []
    for n, key in enumerate(keys, 1):
        filename = str(tmp_path / f"book-{n}.json")
        main(["export", "--binding", binding, "--track", str(n), "--output", filename])
        package = json.loads((tmp_path / f"book-{n}.json").read_text())
        assert set(package) == {"binding", "rubric", "reader_context", "packets", "instruction"}
        assert package["reader_context"] == {"genre": "测试"}
        assert (
            package["rubric"] == PROTOCOL and package["binding"]["rubric_sha256"] == PROTOCOL_HASH
        )
        assert set(package["binding"]) == {
            "protocol",
            "assignment_id",
            "text_sha256",
            "ordered_hash",
            "rubric_sha256",
            "packet_count",
            "reader_context_sha256",
            "judge_sha256",
        }
        assert package["binding"]["packet_count"] == len(package["packets"]) > 1
        assert all(j["session_id"] not in json.dumps(package) for j in judges)
        assert external_book_review.export_assignment(*scope, track=n, packet=2)["packets"] == [
            package["packets"][1]
        ]
        data = assessment(package, veto=outcome == "veto" and n == 2)
        # Confirm split-file handoff reconstructs the same blind edition and that even an
        # uncited text edit in an otherwise well-formed export is caught before signing.
        slices = [{**package, "packets": [pkt]} for pkt in package["packets"]]
        assert external_book_review.check_export(slices, data) == package["binding"]
        changed = deepcopy(slices)
        last = changed[-1]["packets"][0]["passages"][-1]
        last["text"] += "篡"
        last["end"] += 1
        with pytest.raises(DomainError) as altered:
            external_book_review.check_export(changed, data)
        assert altered.value.code == "INVALID_REVIEW_EVIDENCE"
        if outcome == "missing_passage" and n == 2:
            data["observations"][-1] = []
        dest = str(tmp_path / f"signed-{n}.json")
        sign = [
            "sign",
            "--key",
            key,
            "--assignment",
            filename,
            "--assessment",
            private_json(tmp_path / f"assessment-{n}.json", data),
            "--output",
            dest,
        ]
        if outcome == "missing_passage" and n == 2:
            with pytest.raises(DomainError):
                main(sign)
            # An external signer can bypass the local CLI; the backend still enforces coverage.
            signer = serialization.load_pem_private_key(Path(key).read_bytes(), password=None)
            body = external_book_review.signed_bytes(package["binding"], **data)
            private_json(
                Path(dest),
                {
                    "binding": package["binding"],
                    **data,
                    "signature": signer.sign(body, ec.ECDSA(hashes.SHA256())).hex(),
                },
            )
        else:
            main(sign)
        envelopes.append(json.loads((tmp_path / f"signed-{n}.json").read_text()))
        payloads.append(package)
        files.append(dest)
    assert payloads[0]["packets"] == payloads[1]["packets"]
    assert envelopes[0]["binding"]["assignment_id"] != envelopes[1]["binding"]["assignment_id"]
    for substitute in (
        envelopes[1]["binding"],
        {**envelopes[0]["binding"], "text_sha256": "0" * 64},
    ):
        with pytest.raises(DomainError) as tamper:
            external_book_review.import_audit(*scope, {**envelopes[0], "binding": substitute})
        assert tamper.value.code == "INVALID_REVIEW_EVIDENCE"
    with pytest.raises(DomainError) as unauthorized:
        external_book_review.import_audit(k, outsider, work, branch, envelopes[0])
    assert unauthorized.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as empty:
        ReleaseService(k).freeze(owner, request, "before-audits")
    assert empty.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    first = main(["import", "--binding", binding, "--audit", files[0]])
    assert first == external_book_review.import_audit(*scope, envelopes[0])
    # A second valid signature over a changed opinion cannot replace the original.
    replacement = assessment(payloads[0], veto=True)
    signer = serialization.load_pem_private_key(Path(keys[0]).read_bytes(), password=None)
    altered = {
        **replacement,
        "binding": envelopes[0]["binding"],
        "signature": signer.sign(
            external_book_review.signed_bytes(envelopes[0]["binding"], **replacement),
            ec.ECDSA(hashes.SHA256()),
        ).hex(),
    }
    with pytest.raises(DomainError) as duplicate:
        external_book_review.import_audit(*scope, altered)
    assert duplicate.value.code == "REVIEW_ALREADY_SUBMITTED"
    assert external_book_review.export_assignment(*scope, track=2) == payloads[1]
    assert book_review.review_status(*scope)["result"] is None
    with pytest.raises(DomainError) as single:
        ReleaseService(k).freeze(owner, request, "one-audit")
    assert single.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    second = main(["import", "--binding", binding, "--audit", files[1]])
    assert second == external_book_review.import_audit(*scope, envelopes[1])
    status = main(["status", "--binding", binding, "--reports"])
    assert status["review_source"] == "DEVELOPMENT_PLATFORM"
    with k.db.uow(write=False) as repo:
        tasks = repo.many("tasks", {"work_id": work})
        assert not repo.many("call_attempts", {"task_id": [task["task_id"] for task in tasks]})
    if outcome == "missing_passage":
        assert (
            second["status"] == "REJECTED"
            and status["state"] == "BLOCKED"
            and status["result"] is None
        )
    else:
        assert status["state"] == "ASSESSED" and status["result"]["scores"] == [100, 100]
        for reader, env in zip(status["readers"], envelopes, strict=True):
            assert reader["assessment"]["signed_audit_ref"] == reader["audit_ref"]
            assert reader["complete_packets"] == registration["packets"]
            with k.db.uow(write=False) as repo:
                assert k.artifact(repo, reader["audit_ref"], work)["payload"] == env
    if outcome == "pass":
        release = ReleaseService(k).freeze(owner, request, "externally-reviewed")
        book = book_review.export_reviewed_book(*scope, release["release_ref"])
        assert book["result"]["status"] == "QUALITY_TARGET_MET"
        assert book["text_sha256"] == registration["text_sha256"]
        assert book["score_refs"] == [first["score_ref"], second["score_ref"]]
    else:
        if outcome == "veto":
            assert status["result"]["status"] == "P6_QUALITY_NOT_MET"
        with pytest.raises(DomainError) as failed:
            ReleaseService(k).freeze(owner, request, "failed-review")
        assert failed.value.code == "INDEPENDENT_REVIEW_REQUIRED"
