"""Explicit engineering fixture for the offline protocol; no model experiment."""
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid5

from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from .protocol import controls_signature
from .store import EvaluationStore, _immutable_file

NAMESPACE = UUID("841a89ad-8862-4baa-9f97-1d15d23e4f38")
STAMP = "2026-11-10T00:00:00+00:00"


def uid(label):
    return str(uuid5(NAMESPACE, label))


def event(kind, label, **values):
    return {"schema_version": "P3-EVAL-1", "event_id": uid("event:" + label), "observed_at": STAMP,
            "event_type": kind, **values}


def prepare_example(root):
    store = EvaluationStore(root)
    world = {"work_id": uid("fixture-world"), "branch_id": uid("fixture-branch"), "world_revision_id": uid("fixture-revision")}
    specification = store.put({"origin": "ENGINEERING_SPECIFICATION", "world_ref": world,
        "facts": [{"id": "grain-balance", "initial_bags": "100", "delivered_bags": "30", "remaining_bags": "70"}],
        "note": "This small independently written fixture is not human literary gold or a real event export."})
    manifest = store.put({"origin": "ENGINEERING_FIXTURE", "world_ref": world, "original_specification_ref": specification})
    brief = store.put("写完一个有开端和收束的短卷：守仓人决定分出三十袋粮食，另一人在河岸接过责任。保持人物的矛盾心情；不把事成之后的轻松当作痛苦消失。", "text/plain; charset=utf-8")
    model = store.put({"model": "engineering-fixture-no-model", "execution_kind": "ENGINEERING_FIXTURE", "temperature": "0"})
    permissions = store.put({"world_ref": world, "roles": ["writer", "literary_editor"], "tools": [], "network": False})
    prompt = store.put({"template_version": "engineering-v1", "instruction": "Use the shared world, brief and exact output budget."})
    memory = store.put("工程文学档案夹具：守仓人对分粮既宽慰又不安，表面言语少于内心活动。来源是原始规格；这不是文学判断金标准。", "text/plain; charset=utf-8")
    plan = {"schema_version": "P3-EVAL-1", "experiment_id": uid("experiment"), "sample_id": uid("whole-fixture-volume"),
        "unit": "WHOLE_VOLUME", "factor": "MEMORY", "randomization_seed": 1701, "tuning_world_refs": [],
        "inference_policy": "NO_AUTOMATIC_LITERARY_WINNER",
        "controls": {"world_ref": world, "input_manifest_ref": manifest, "creative_brief_ref": brief,
            "model_id": "engineering-fixture-no-model", "model_config_sha256": model["sha256"],
            "permission_manifest_sha256": permissions["sha256"], "prompt_template_sha256": prompt["sha256"], "style_refs": [],
            "target_codepoints_min": 60, "target_codepoints_max": 2000, "scene_count_min": 2, "scene_count_max": 2,
            "budget": {"max_calls": 4, "max_total_tokens": 4000, "max_cost_usd": "1"}},
        "conditions": [
            {"condition_id": uid("condition-without-memory"), "memory_mode": "NONE", "editing_mode": "WHOLE_VOLUME", "material_refs": []},
            {"condition_id": uid("condition-with-memory"), "memory_mode": "EVIDENCE_LITERARY_MEMORY", "editing_mode": "WHOLE_VOLUME", "material_refs": [memory]}]}
    store.initialize(plan)
    return store, plan, specification


def start(store, plan, label, condition_index, *, reserved_tokens=1000, reserved_cost="0.5"):
    request = store.put({"origin": "ENGINEERING_FIXTURE", "attempt": label,
                         "controls_sha256": controls_signature(plan), "condition_id": plan["conditions"][condition_index]["condition_id"]})
    value = event("ATTEMPT_STARTED", "start:" + label, attempt_id=uid("attempt:" + label),
        condition_id=plan["conditions"][condition_index]["condition_id"], controls_sha256=controls_signature(plan),
        execution_kind="ENGINEERING_FIXTURE", reserved_tokens=reserved_tokens, reserved_cost_usd=reserved_cost,
        request_ref=request, source_execution_refs=[])
    store.append(value)
    return value


def candidate(store, scenes):
    refs = [store.put(text, "text/plain; charset=utf-8") for text in scenes]
    return {"text_ref": store.put("\n\n".join(scenes), "text/plain; charset=utf-8"), "ordered_scene_refs": refs}


def finish(store, attempt, *, outcome="SUCCEEDED", text=None, cost="0.1", unknown=False):
    result = {"outcome": outcome, "usage": {"input_tokens": None if unknown else 100,
        "output_tokens": None if unknown else 200, "cached_input_tokens": None if unknown else 0},
        "actual_cost_usd": None if unknown else cost, "latency_ms": 20,
        "provider_receipt": None if unknown else "ENGINEERING_FIXTURE:" + attempt["attempt_id"],
        "response_ref": None if unknown else store.put({"origin": "ENGINEERING_FIXTURE", "outcome": outcome, "candidate": text}),
        "candidate": text, "error_code": "SIMULATED_TIMEOUT" if unknown else None if outcome == "SUCCEEDED" else "SIMULATED_FAILURE"}
    value = event("ATTEMPT_FINISHED", "finish:" + attempt["attempt_id"], attempt_id=attempt["attempt_id"], result=result)
    store.append(value)
    return result


def select(store, attempt, result, disposition="SELECTED"):
    source = store.put({"origin": "ENGINEERING_FIXTURE", "reason": "Exercise retained selection/discard evidence, not literary judgement."})
    return store.append(event("CANDIDATE_DECISION", "decision:" + attempt["attempt_id"],
        attempt_id=attempt["attempt_id"], candidate_sha256=result["candidate"]["text_ref"]["sha256"], disposition=disposition,
        reason="工程夹具的固定选择；没有宣称人工审美评价。", evidence_refs=[source]))


def populate_example(store, plan, specification):
    failed = start(store, plan, "retained-failure", 0)
    finish(store, failed, outcome="FAILED", cost="0.03")
    first = start(store, plan, "first-selected-volume", 0)
    text_a = candidate(store, [
        "一、仓门\n守仓人点到第三十袋时停了手。门外的人没有催，他却忽然觉得沉默比催促更重。他把账本压在掌下，说剩下七十袋，还够熬过几夜。",
        "二、河岸\n接粮的人沿着河岸走来，肩上的绳痕没有淡。守仓人把最后一袋推过去，两人都笑了一下，又都没有再笑。船离岸时，他仍捏着那张收条，直到纸角被汗浸软。"])
    result_a = finish(store, first, text=text_a)
    select(store, first, result_a)
    rejected = start(store, plan, "retained-discarded-volume", 0)
    discarded = candidate(store, [
        "一、仓门\n守仓人把三十袋粮食交出去，算清剩下七十袋。大家点头，他也跟着点头，旧日的担忧暂时被工作压了下去。门外有风吹过。",
        "二、河岸\n船装好粮食离开。他站在河岸看了片刻，想说的话一句也没有说。回到仓里，他重新挂好锁，记下今天的数字。"])
    select(store, rejected, finish(store, rejected, text=discarded, cost="0.05"), "REJECTED")
    second = start(store, plan, "second-reconciled-volume", 1)
    unresolved = finish(store, second, outcome="UNKNOWN", unknown=True)
    text_b = candidate(store, [
        "一、仓门\n第三十袋落在车上，守仓人却又数了一遍。他知道剩下七十袋，也知道门外的人听不出数字背后的害怕。他把账本合起来，手掌还留在封面上。",
        "二、河岸\n接粮的人把破绳打了一个新结。两人谈起从前翻过的小船，笑声才起，便被水声接了过去。守仓人没有追回那声笑，也没有把它当作释怀。他把收条折好，第一次请对方明早来一起清点。"])
    resolved = {**deepcopy(unresolved), "outcome": "SUCCEEDED", "candidate": text_b,
        "usage": {"input_tokens": 100, "output_tokens": 200, "cached_input_tokens": 0},
        "provider_receipt": "ENGINEERING_FIXTURE:" + second["attempt_id"],
        "response_ref": store.put({"origin": "ENGINEERING_FIXTURE", "recovered_candidate": text_b}),
        "error_code": None}
    reconciliation = store.put({"origin": "ENGINEERING_FIXTURE", "result_recovered": True, "billing_receipt_available": False})
    store.append(event("ATTEMPT_RECONCILED", "reconcile-second", attempt_id=second["attempt_id"],
        result=resolved, reconciliation_evidence_refs=[reconciliation]))
    select(store, second, resolved)
    # A failed preflight remains a retained rejection. No call is dispatched.
    try:
        start(store, plan, "blocked-by-unknown-cost-reservation", 1, reserved_cost="0.6")
    except DomainError as exc:
        if exc.code != "EVALUATION_BUDGET_EXHAUSTED":
            raise
    intervention = store.put("ENGINEERING_FIXTURE: no real person made this editorial selection.", "text/plain; charset=utf-8")
    store.append(event("HUMAN_INTERVENTION", "fixture-intervention", attempt_id=None,
        origin="ENGINEERING_FIXTURE", description="记录人工介入格式的工程占位；不计真实人工审核。", source_ref=intervention))
    provenance = store.put({"schema_version": "P3-EVAL-1", "source_kind": "ENGINEERING_SPECIFICATION",
        "world_ref": plan["controls"]["world_ref"], "source_refs": [specification],
        "source_created_at": STAMP, "upstream_identifier": "engineering-original-grain-specification-v1",
        "authorship_claim": "Engineering fixture authored separately from the two retained drafts.",
        "generated_by_experiment": False,
        "verification_status": "SOURCE_HASHES_CHECKED_IDENTITY_AND_INDEPENDENCE_NOT_AUTHENTICATED"})
    store.append(event("GOLD_REGISTERED", "gold", gold_id=uid("gold"), purpose="FACTUAL_RETRIEVAL",
        source_kind="ENGINEERING_SPECIFICATION", world_ref=plan["controls"]["world_ref"], source_refs=[specification],
        provenance_ref=provenance, generated_by_experiment=False,
        independence_claim="Original engineering arithmetic; not a system summary, literary verdict or authenticated human gold.",
        verification_status="PROVENANCE_FILES_CHECKED_IDENTITY_AND_INDEPENDENCE_NOT_AUTHENTICATED"))
    return {"first": first, "second": second, "rejected": rejected, "first_result": result_a, "second_result": resolved}


def run_demo(output_directory):
    root = Path(output_directory)
    store, plan, specification = prepare_example(root / "experiment")
    populate_example(store, plan, specification)
    store.blind(root / "reviewer", root / "coordinator")
    report = store.report()
    _immutable_file(root / "report.json", canonical_bytes(report) + b"\n")
    _immutable_file(root / "checkpoint.json", canonical_bytes(store.checkpoint()) + b"\n")
    return report
