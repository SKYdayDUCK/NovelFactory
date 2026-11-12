"""Paired local engineering execution and restart on real authorized NF manuscript versions."""

import pytest
from adaptive_helpers import setup_adaptive_case
from test_harness_review_workflow import _args

from novel_factory.application import adaptive_evaluation
from novel_factory.application.adaptive_evaluation import AdaptiveEvaluationService
from novel_factory.application.adaptive_instructions import AdaptiveInstructionService
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture
def experiment(kernel_env, tmp_path, monkeypatch):
    data = setup_adaptive_case(kernel_env["kernel"], tmp_path, monkeypatch)
    data["engine"] = AdaptiveEvaluationService(data["kernel"])
    return data


def _propose(e, **patch):
    args = {**e["args"], **patch}
    return AdaptiveInstructionService(e["kernel"]).propose(e["admin"], e["wr"], **args)["candidate_ref"]


def _refs(result):
    return {k: result[k] for k in ("candidate_ref", "engineering_report_ref", "run_manifest_ref")}


def _run(e, candidate_ref, key="engine"):
    return e["engine"].run(e["admin"], e["wr"], candidate_ref=candidate_ref, idempotency_key=key)


def test_replays_originals_with_blind_pairs_and_no_duplicate_outputs(experiment, monkeypatch):
    e = experiment
    candidate = _propose(e)
    engine = e["engine"]
    saved = engine._row
    visits = []

    def stop(*args):
        row = saved(*args)
        visits.append(row[0])
        if len(visits) == 2:
            raise InterruptedError("injected worker interruption after durable pair")
        return row

    monkeypatch.setattr(engine, "_row", stop)
    with pytest.raises(InterruptedError):
        _run(e, candidate)
    monkeypatch.setattr(engine, "_row", saved)
    result = _run(e, candidate)
    assert result["status"] == "COMPLETE" and result["eligible"], result
    assert result == _run(e, candidate)
    assert result == _run(e, candidate, "second-receipt")
    with e["kernel"].db.uow() as repo:
        report = engine.require_release(repo, e["wr"], **_refs(result))
        assert report["literary_status"] == "UNPROVEN"
        assert report["quality"]["improved"] and report["quality"]["held_out_negatives"]
        assert (
            report["arm_totals"]["CONTROL"]["false_negatives"]
            > report["arm_totals"]["TREATMENT"]["false_negatives"]
        )
        rows = report["results"]
        assert len(rows) == 4 and len({r["blind_id"] for r in rows}) == 4
        for case in {r["case_id"] for r in rows}:
            control, treatment = sorted((r for r in rows if r["case_id"] == case), key=lambda r: r["arm_id"])
            assert control["input_sha256"] == treatment["input_sha256"]
            assert control["output"]["system_sha256"] != treatment["output"]["system_sha256"]
            assert control["seed"] == treatment["seed"] == 2048
            assert control["latency_ns"] > 0 and treatment["latency_ns"] > 0
            assert (
                control["amount"] == treatment["amount"] == "0"
                and not control["attempt_refs"]
                and not treatment["attempt_refs"]
            )
        manifest = e["kernel"].artifact(repo, result["run_manifest_ref"], e["wr"]["work_id"])["payload"]
        assert {r["artifact_revision_id"] for r in visits} <= {
            r["artifact_revision_id"] for r in manifest["measurement_refs"]
        }
        assert not repo.many("call_attempts", {"task_id": e["source"].task["task_id"]})
        assert e["kernel"].head(repo, e["wr"]["work_id"], e["wr"]["branch_id"]) == e["wr"]


def test_incomplete_allocation_and_failed_execution_are_retained(experiment, monkeypatch):
    e = experiment
    quota = {**e["args"]["quota"], "max_attempts": 2}
    candidate = _propose(e, quota=quota)
    original = adaptive_evaluation._execute

    def fail_one(arm, material, kind):
        assert "expected_findings" not in material and "classification" not in material
        if arm.get("body"):
            raise RuntimeError("deliberate engineering failure")
        return original(arm, material, kind)

    monkeypatch.setattr(adaptive_evaluation, "_execute", fail_one)
    result = _run(e, candidate)
    assert result["status"] == "INCOMPLETE" and not result["eligible"]
    with e["kernel"].db.uow() as repo:
        report = e["engine"].inspect(repo, e["wr"], **_refs(result))
        assert sorted(r["status"] for r in report["results"]) == [
            "ATTEMPT_LIMIT",
            "ATTEMPT_LIMIT",
            "ENGINE_FAILURE",
            "SUCCEEDED",
        ]
        assert sum(r["executed"] for r in report["results"]) == 2
        with pytest.raises(DomainError) as blocked:
            e["engine"].require_release(repo, e["wr"], **_refs(result))
        assert blocked.value.code == "ADAPTIVE_EVALUATION_REJECTED"


def test_regression_and_calibration_only_runs_cannot_be_released(experiment):
    e = experiment
    # A second independent interpretation retains the other refrain. A treatment
    # that rediscovers both now creates an extra alert on this held-out version.
    job, ref, review_args, _ = e["records"][2]
    k = e["kernel"]
    with k.db.uow(write=False) as repo:
        from novel_factory.application.creative_review import _review_history

        _, details = _review_history(k, repo, e["wr"]["work_id"], ref)
        original = details["reader_observations"]
    assert len(original) == 2
    interpretation = [original[0], {**original[1], "disposition": "PRESERVE", "requested_change": None}]
    alternate = CreativeReviewService(k).review(
        *_args(job), **{**review_args, "observations": interpretation, "idempotency_key": "retain-second"}
    )
    candidate = _propose(e, evaluation_report_refs=[alternate["report_ref"], e["evaluation_report_refs"][1]])
    result = _run(e, candidate)
    assert result["status"] == "COMPLETE" and not result["eligible"]
    with k.db.uow() as repo:
        report = e["engine"].inspect(repo, e["wr"], **_refs(result))
        assert report["quality"]["regressed"] == [alternate["report_ref"]["artifact_revision_id"]]
        assert "CASE_REGRESSION" in report["quality"]["reasons"]
    overlap = _propose(e, evaluation_report_refs=None, idempotency_key="calibration")
    measured = _run(e, overlap, "calibration")
    with k.db.uow() as repo:
        report = e["engine"].inspect(repo, e["wr"], **_refs(measured))
        assert not any(r["held_out"] for r in report["results"])
        assert not report["eligible"] and "NO_HELD_OUT_GAIN" in report["quality"]["reasons"]


def test_expired_preregistration_records_every_unexecuted_arm(experiment, monkeypatch):
    from datetime import datetime, timedelta

    e = experiment
    ref = _propose(e)
    deadline = datetime.fromisoformat(e["args"]["quota"]["deadline"])

    class AfterDeadline(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline + timedelta(seconds=1)

    monkeypatch.setattr(adaptive_evaluation, "datetime", AfterDeadline)
    measured = _run(e, ref)
    with e["kernel"].db.uow() as repo:
        report = e["engine"].inspect(repo, e["wr"], **_refs(measured))
        assert report["status"] == "INCOMPLETE" and not report["eligible"]
        assert len(report["results"]) == 4
        assert all(
            r["status"] == "DEADLINE_EXCEEDED"
            and not r["executed"]
            and r["output"] is None
            and r["latency_ns"] == 0
            and r["amount"] == "0"
            for r in report["results"]
        )
