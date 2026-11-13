"""P3-03 offline engineering protocol tests; no models or literary scores."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.evaluation.example import (
    candidate, event, finish, populate_example, prepare_example, run_demo, select, start, uid,
)
from novel_factory.evaluation.protocol import controls_signature, validate_plan

pytestmark = pytest.mark.unit


@pytest.fixture
def prepared(tmp_path):
    return prepare_example(tmp_path / "experiment")


@pytest.fixture
def populated(prepared):
    store, plan, spec = prepared
    values = populate_example(store, plan, spec)
    return store, plan, values


@pytest.mark.parametrize("change,code", [
    (lambda p: p.update(extra=True), "INVALID_EVALUATION_INPUT"),
    (lambda p: p["conditions"][1].update(editing_mode="CHAPTER_ONLY"), "EVALUATION_CONTROLS_MISMATCH"),
    (lambda p: p.update(tuning_world_refs=[p["controls"]["world_ref"]]), "EVALUATION_SPLIT_LEAK"),
    (lambda p: p["controls"].update(target_codepoints_min=3000), "EVALUATION_CONTROLS_MISMATCH"),
], ids=["unknown-field", "two-variables-changed", "tuning-world-leak", "inconsistent-target"])
def test_strict_plan_preserves_one_factor_and_fixed_controls(prepared, change, code):
    _, plan, _ = prepared
    altered = deepcopy(plan)
    change(altered)
    with pytest.raises(DomainError) as error:
        validate_plan(altered)
    assert error.value.code == code


def test_editing_pair_requires_identical_memory_and_materials(prepared):
    _, plan, _ = prepared
    plan["factor"] = "VOLUME_EDITING"
    plan["conditions"][0]["memory_mode"] = plan["conditions"][1]["memory_mode"]
    plan["conditions"][0]["material_refs"] = deepcopy(plan["conditions"][1]["material_refs"])
    plan["conditions"][0]["editing_mode"] = "CHAPTER_ONLY"
    assert validate_plan(plan) == plan
    plan["conditions"][0]["material_refs"] = []
    with pytest.raises(DomainError) as error:
        validate_plan(plan)
    assert error.value.code == "EVALUATION_CONTROLS_MISMATCH"


def test_attempt_cannot_change_model_world_permissions_or_budget_fingerprint(prepared):
    store, plan, _ = prepared
    req = store.put({"origin": "ENGINEERING_FIXTURE", "request": "wrong model"})
    wrong = event("ATTEMPT_STARTED", "wrong-controls", attempt_id=uid("wrong-controls"),
        condition_id=plan["conditions"][0]["condition_id"], controls_sha256="f" * 64,
        execution_kind="ENGINEERING_FIXTURE", reserved_tokens=100, reserved_cost_usd="0.1", request_ref=req, source_execution_refs=[])
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            store.append(wrong)
        assert error.value.code == "EVALUATION_CONTROLS_MISMATCH"
    current = store.snapshot()
    assert current["event_count"] == 1 and not current["state"]["attempts"]
    assert current["events"][0]["event_type"] == "ATTEMPT_START_REJECTED"
    corrected_same_identity = {**wrong, "controls_sha256": controls_signature(plan)}
    with pytest.raises(DomainError) as error:
        store.append(corrected_same_identity)
    assert error.value.code == "EVALUATION_LEDGER_DUPLICATE"


def test_unknown_cost_keeps_reservation_and_reconciliation_never_erases_failure(prepared):
    store, plan, _ = prepared
    attempt = start(store, plan, "unknown", 0)
    unknown = finish(store, attempt, outcome="UNKNOWN", unknown=True)
    with pytest.raises(DomainError) as error:
        start(store, plan, "over-budget", 0, reserved_cost="0.6")
    assert error.value.code == "EVALUATION_BUDGET_EXHAUSTED"
    report = store.report()
    accounting = report["condition_accounting"][0]
    assert accounting["total_actual_cost_usd"] is None and accounting["unknown_cost_reserved_usd"] == "0.5"
    assert report["rejected_start_count"] == 1
    observed = {**unknown, "outcome": "FAILED", "actual_cost_usd": "0.2",
        "usage": {"input_tokens": 100, "output_tokens": 0, "cached_input_tokens": 0}, "error_code": "RECONCILED_FAILURE"}
    proof = store.put({"origin": "ENGINEERING_FIXTURE", "receipt": "final failure with recorded cost"})
    store.append(event("ATTEMPT_RECONCILED", "settle-unknown", attempt_id=attempt["attempt_id"],
                       result=observed, reconciliation_evidence_refs=[proof]))
    assert store.report()["attempts"][0]["observed_outcomes"] == ["UNKNOWN", "FAILED"]
    assert store.report()["condition_accounting"][0]["total_actual_cost_usd"] == "0.2"
    start(store, plan, "after-settlement", 0, reserved_cost="0.6")


def test_known_cost_candidate_and_usage_cannot_be_rewritten(populated):
    store, _, values = populated
    altered = deepcopy(values["second_result"])
    altered["usage"]["output_tokens"] = 1
    evidence = store.put({"origin": "ENGINEERING_FIXTURE", "invalid_reconciliation": True})
    with pytest.raises(DomainError) as error:
        store.append(event("ATTEMPT_RECONCILED", "rewrite-known", attempt_id=values["second"]["attempt_id"],
            result=altered, reconciliation_evidence_refs=[evidence]))
    assert error.value.code == "EVALUATION_RECONCILIATION_DENIED"
    assert store.report()["condition_accounting"][1]["total_actual_cost_usd"] is None


@pytest.mark.parametrize("damage", ["modified-row", "partial-tail", "changed-object"])
def test_corruption_fails_closed_and_is_not_auto_repaired(prepared, damage):
    store, plan, _ = prepared
    started = start(store, plan, "integrity", 0)
    path = store.root / "ledger.jsonl"
    original = path.read_bytes()
    if damage == "modified-row":
        row = json.loads(original)
        row["event"]["reserved_cost_usd"] = "0"
        path.write_bytes(canonical_bytes(row) + b"\n")
    elif damage == "partial-tail":
        path.write_bytes(original[:-4])
    else:
        (store.objects / started["request_ref"]["sha256"]).write_bytes(b"corrupt source")
    retained = path.read_bytes()
    with pytest.raises(DomainError):
        store.snapshot()
    assert path.read_bytes() == retained


def test_external_checkpoint_detects_valid_tail_truncation(prepared):
    store, plan, _ = prepared
    start(store, plan, "one", 0)
    one = (store.root / "ledger.jsonl").read_bytes()
    start(store, plan, "two", 1)
    checkpoint = store.checkpoint()
    (store.root / "ledger.jsonl").write_bytes(one)
    assert store.snapshot()["event_count"] == 1  # Hash chains alone cannot prove completeness.
    with pytest.raises(DomainError) as error:
        store.snapshot(expected_head=checkpoint["head_sha256"])
    assert error.value.code == "EVALUATION_CHECKPOINT_MISMATCH"


@pytest.mark.parametrize("which", ["memory", "generated-draft"])
def test_own_summary_or_generated_text_cannot_be_gold(populated, which):
    store, plan, values = populated
    original = store.snapshot()["state"]["gold"][0]
    ref = (plan["conditions"][1]["material_refs"][0] if which == "memory"
           else values["first_result"]["candidate"]["text_ref"])
    provenance = json.loads(store.read(original["provenance_ref"]))
    provenance["source_refs"] = [ref]
    invalid = {**original, "event_id": uid("circular-gold-event"), "gold_id": uid("circular-gold"),
               "source_refs": [ref], "provenance_ref": store.put(provenance)}
    with pytest.raises(DomainError) as error:
        store.append(invalid)
    assert error.value.code == "EVALUATION_CIRCULAR_GOLD"
    assert len(store.snapshot()["state"]["gold"]) == 1


def test_independent_gold_provenance_must_match_exact_sources(populated):
    store, _, _ = populated
    gold = store.snapshot()["state"]["gold"][0]
    forged = {**gold, "event_id": uid("gold-mismatch"), "gold_id": uid("gold-mismatch-id"),
              "source_refs": [store.put("A different source.", "text/plain; charset=utf-8")]}
    with pytest.raises(DomainError) as error:
        store.append(forged)
    assert error.value.code == "EVALUATION_GOLD_MISMATCH"
    with pytest.raises(DomainError):
        store.append({**forged, "generated_by_experiment": True})


def test_seeded_blind_pack_is_reproducible_and_separate_from_key(populated, tmp_path):
    store, plan, _ = populated
    output = store.blind(tmp_path / "reviewer", tmp_path / "coordinator")
    assert store.blind(tmp_path / "reviewer", tmp_path / "coordinator") == output
    packet = json.loads((tmp_path / "reviewer" / "review.json").read_bytes())
    key = json.loads((tmp_path / "coordinator" / "allocation.json").read_bytes())
    visible = b"\n".join(p.read_bytes() for p in (tmp_path / "reviewer").iterdir())
    for hidden in (plan["experiment_id"], plan["sample_id"], plan["controls"]["model_id"],
                   *[c["condition_id"] for c in plan["conditions"]]):
        assert hidden.encode() not in visible
    assert {p.name for p in (tmp_path / "reviewer").iterdir()} == {"A.txt", "B.txt", "brief.txt", "review.json", "README.txt"}
    from novel_factory.infrastructure.private_files import read_private_bytes
    allocation = tmp_path / "coordinator" / "allocation.json"
    assert read_private_bytes(allocation, 1_000_000) == allocation.read_bytes()
    if os.name != "nt":
        assert stat.S_IMODE(allocation.stat().st_mode) == 0o600
    assert key["seed"] == plan["randomization_seed"] and key["delivery_status"].startswith("PREPARED_NOT_DELIVERED")
    assert packet["status"] == "READY_FOR_REVIEW"
    report2 = run_demo(tmp_path / "second-independent-store")
    assert report2["literary_inference_status"] == "BLOCKED_LITERARY_INFERENCE"
    key2 = json.loads((tmp_path / "second-independent-store" / "coordinator" / "allocation.json").read_bytes())
    assert key2["mapping"] == key["mapping"]


def test_blind_pack_rejects_colocated_or_polluted_reviewer_directory(populated, tmp_path):
    store, _, _ = populated
    with pytest.raises(DomainError) as error:
        store.blind(tmp_path / "same", tmp_path / "same" / "coordinator")
    assert error.value.code == "EVALUATION_BLINDING_LEAK"
    reviewer = tmp_path / "polluted"
    reviewer.mkdir()
    (reviewer / "condition-key.json").write_text("retained prior file")
    with pytest.raises(DomainError) as error:
        store.blind(reviewer, tmp_path / "separate-key")
    assert error.value.code == "EVALUATION_BLINDING_LEAK"
    assert (reviewer / "condition-key.json").read_text() == "retained prior file"


def test_whole_original_and_length_are_checked_without_dropping_bad_draft(prepared):
    store, plan, _ = prepared
    for index in (0, 1):
        attempt = start(store, plan, "too-short-" + str(index), index)
        draft = candidate(store, ["开头。", "结尾。"])
        result = finish(store, attempt, text=draft)
        select(store, attempt, result)
    report = store.report()
    assert report["readiness"] == "BLOCKED_PAIR_PREPARATION" and report["preparation_blocker"] == "EVALUATION_TARGET_MISMATCH"
    assert len(report["attempts"]) == 2 and all(a["candidate_ref"] for a in report["attempts"])


def test_actual_overrun_is_retained_and_blocks_comparison(prepared):
    store, plan, _ = prepared
    attempt = start(store, plan, "expensive-failure", 0)
    finish(store, attempt, outcome="FAILED", cost="1.2")
    report = store.report()
    assert report["condition_accounting"][0]["cap_breached"] is True
    assert report["condition_accounting"][0]["known_cost_usd"] == "1.2"
    assert report["attempts"][0]["outcome"] == "FAILED"


def test_concurrent_append_preserves_sequence_and_all_started_attempts(prepared):
    store, plan, _ = prepared
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda i: start(store, plan, "concurrent-" + str(i), i % 2,
            reserved_tokens=100, reserved_cost="0.1"), range(8)))
    current = store.snapshot()
    assert current["event_count"] == len(values) == 8
    assert [r["sequence"] for r in current["records"]] == list(range(1, 9))
    assert len(current["state"]["attempts"]) == 8


def test_claimed_human_reading_never_becomes_authenticated_literary_winner(populated, tmp_path):
    store, _, _ = populated
    packet = store.blind(tmp_path / "review", tmp_path / "key")
    raw = store.put("A caller says they read both full originals. This claim is not independently authenticated.", "text/plain; charset=utf-8")
    store.append(event("HUMAN_READING_RECORDED", "reading-declaration", reader_pseudonym="unverified-reader",
        origin="EXTERNAL_HUMAN_INPUT_UNVERIFIED", blind_packet_sha256=packet["packet_ref"]["sha256"],
        full_original_reading_claim=True, report_ref=raw, dimensions=["CHARACTER_CREDIBILITY", "LANGUAGE"],
        verification_status="RAW_DECLARATION_REQUIRES_INDEPENDENT_VERIFICATION"))
    report = store.report()
    assert report["readiness"] == "READY_FOR_REVIEW"
    assert report["literary_inference_status"] == "BLOCKED_LITERARY_INFERENCE"
    assert report["execution_counts"]["REAL_MODEL_DECLARED"] == 0
    assert "winner" not in report and "improvement_percent" not in report
    assert "HUMAN_IDENTITY_AND_FULL_READING_NOT_INDEPENDENTLY_AUTHENTICATED" in report["literary_inference_blockers"]


def test_cli_verifies_external_checkpoint_and_has_no_model_route(populated):
    store, _, _ = populated
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(repo / "scripts" / "run_p3_evaluation.py"), "verify",
        "--store", str(store.root), "--expected-head", store.checkpoint()["head_sha256"]],
        text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["history_completeness"] == "MATCHES_SUPPLIED_CHECKPOINT"
    assert store.report()["provider_calls_made_by_evaluation_tool"] == 0


def test_later_generated_candidate_cannot_reuse_previously_registered_gold(populated):
    store, plan, _ = populated
    gold = store.snapshot()["state"]["gold"][0]
    original_ref = gold["source_refs"][0]
    original = store.read(original_ref).decode("utf-8")
    attempt = start(store, plan, "later-circular-output", 0, reserved_tokens=10, reserved_cost="0.01")
    draft = candidate(store, [original])
    head = store.checkpoint()
    with pytest.raises(DomainError) as error:
        finish(store, attempt, text=draft)
    assert error.value.code == "EVALUATION_CIRCULAR_GOLD"
    assert store.checkpoint() == head
