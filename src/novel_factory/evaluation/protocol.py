"""Deterministic controls, lifecycle, budget and provenance checks.

This ledger records observations. It neither calls a model nor replaces the
production TaskService budget, provider receipt or execution authority.
"""
from copy import deepcopy
from decimal import Decimal

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from .contracts import validate


def require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def validate_plan(plan):
    value = validate("EvaluationPlan", plan)
    controls = value["controls"]
    require(controls["target_codepoints_min"] <= controls["target_codepoints_max"]
            and controls["scene_count_min"] <= controls["scene_count_max"],
            "EVALUATION_CONTROLS_MISMATCH", "Target bounds are inconsistent")
    if value["unit"] == "CROSS_CHAPTER_SEQUENCE":
        require(controls["scene_count_min"] >= 2, "EVALUATION_UNIT_MISMATCH", "Cross-chapter samples need multiple ordered texts")
    left, right = value["conditions"]
    require(left["condition_id"] != right["condition_id"], "EVALUATION_CONTROLS_MISMATCH", "Paired conditions need distinct identities")
    if value["factor"] == "MEMORY":
        require(left["memory_mode"] != right["memory_mode"] and left["editing_mode"] == right["editing_mode"],
                "EVALUATION_CONTROLS_MISMATCH", "A memory pair changes only the memory condition")
    else:
        require(left["editing_mode"] != right["editing_mode"] and left["memory_mode"] == right["memory_mode"]
                and left["material_refs"] == right["material_refs"],
                "EVALUATION_CONTROLS_MISMATCH", "An editing pair holds memory and its material constant")
    for condition in value["conditions"]:
        require(condition["memory_mode"] != "NONE" or not condition["material_refs"],
                "EVALUATION_CONTROLS_MISMATCH", "No-memory control cannot carry auxiliary memory")
        require(condition["memory_mode"] == "NONE" or bool(condition["material_refs"]),
                "EVALUATION_CONTROLS_MISMATCH", "Memory condition needs its exact material manifest")
    require(all(wr["work_id"] != controls["world_ref"]["work_id"] for wr in value["tuning_world_refs"]),
            "EVALUATION_SPLIT_LEAK", "Tuning and evaluation worlds must be separate")
    return value


def controls_signature(plan):
    return fingerprint(plan["controls"])


def _charge(attempt):
    result = attempt.get("result")
    if result is None:
        return attempt["reserved_tokens"], Decimal(attempt["reserved_cost_usd"])
    usage = result["usage"]
    tokens = (usage["input_tokens"] + usage["output_tokens"]
              if usage["input_tokens"] is not None and usage["output_tokens"] is not None else attempt["reserved_tokens"])
    amount = Decimal(result["actual_cost_usd"] if result["actual_cost_usd"] is not None else attempt["reserved_cost_usd"])
    return tokens, amount


def state_for(plan, events):
    """Replay every record, including errors, rejected text and UNKNOWN costs."""
    plan = validate_plan(plan)
    condition_ids = {c["condition_id"] for c in plan["conditions"]}
    attempts, decisions, gold, interventions, readings, rejections, packets = {}, {}, [], [], [], [], []
    event_ids, provider_receipts = set(), {}
    budget = plan["controls"]["budget"]
    for raw in events:
        event = validate("EvaluationEvent", raw)
        require(event["event_id"] not in event_ids, "EVALUATION_LEDGER_DUPLICATE", "Event identity repeated inside ledger")
        event_ids.add(event["event_id"])
        kind = event["event_type"]
        if kind == "ATTEMPT_STARTED":
            aid, cid = event["attempt_id"], event["condition_id"]
            require(aid not in attempts and cid in condition_ids, "EVALUATION_ATTEMPT_CONFLICT", "Attempt or condition identity is invalid")
            require(event["controls_sha256"] == controls_signature(plan), "EVALUATION_CONTROLS_MISMATCH", "Attempt does not bind the fixed paired controls")
            require(event["execution_kind"] != "REAL_MODEL_DECLARED" or bool(event["source_execution_refs"]),
                    "EVALUATION_CONTROLS_MISMATCH", "Declared real execution needs original production evidence; declaration alone is insufficient")
            prior = [a for a in attempts.values() if a["condition_id"] == cid]
            tokens = sum(_charge(a)[0] for a in prior) + event["reserved_tokens"]
            amount = sum((_charge(a)[1] for a in prior), Decimal(0)) + Decimal(event["reserved_cost_usd"])
            require(len(prior) < budget["max_calls"] and tokens <= budget["max_total_tokens"]
                    and amount <= Decimal(budget["max_cost_usd"]),
                    "EVALUATION_BUDGET_EXHAUSTED", "Attempt exceeds the paired cap; unresolved usage and cost retain their reservations")
            attempts[aid] = deepcopy(event)
            attempts[aid]["results"] = []
        elif kind in {"ATTEMPT_FINISHED", "ATTEMPT_RECONCILED"}:
            aid = event["attempt_id"]
            require(aid in attempts, "EVALUATION_ATTEMPT_CONFLICT", "Outcome has no recorded attempt start")
            attempt, result = attempts[aid], event["result"]
            prior = attempt.get("result")
            if kind == "ATTEMPT_FINISHED":
                require(prior is None, "EVALUATION_ATTEMPT_CONFLICT", "Attempt already has a terminal observation")
            else:
                require(prior is not None and (prior["outcome"] == "UNKNOWN" or prior["actual_cost_usd"] is None
                        or prior["usage"]["input_tokens"] is None or prior["usage"]["output_tokens"] is None),
                        "EVALUATION_RECONCILIATION_DENIED", "Only unresolved outcomes, usage or cost can be reconciled")
                require(prior != result, "EVALUATION_RECONCILIATION_DENIED", "Reconciliation adds no evidence")
                for key in ("actual_cost_usd", "provider_receipt", "candidate", "response_ref"):
                    require(prior[key] is None or prior[key] == result[key],
                            "EVALUATION_RECONCILIATION_DENIED", "Known cost, receipt or original candidate cannot be rewritten")
                for key in prior["usage"]:
                    require(prior["usage"][key] is None or prior["usage"][key] == result["usage"][key],
                            "EVALUATION_RECONCILIATION_DENIED", "Known usage cannot be rewritten")
                require(prior["outcome"] == "UNKNOWN" or prior["outcome"] == result["outcome"],
                        "EVALUATION_RECONCILIATION_DENIED", "Known outcome cannot be rewritten")
                resolved = any(prior[key] is None and result[key] is not None
                               for key in ("actual_cost_usd", "provider_receipt", "candidate", "response_ref"))
                resolved = resolved or any(prior["usage"][key] is None and result["usage"][key] is not None
                                           for key in prior["usage"])
                resolved = resolved or prior["outcome"] == "UNKNOWN" and result["outcome"] != "UNKNOWN"
                require(resolved, "EVALUATION_RECONCILIATION_DENIED", "Reconciliation must resolve unknown evidence, not revise the observed latency")
            require(result["outcome"] != "SUCCEEDED" or result["candidate"] is not None,
                    "EVALUATION_CANDIDATE_MISSING", "Successful production must retain its complete candidate")
            require(result["candidate"] is None or result["response_ref"] is not None,
                    "EVALUATION_CANDIDATE_MISSING", "A candidate needs its original response evidence")
            require(result["outcome"] != "UNKNOWN" or result["actual_cost_usd"] is None,
                    "EVALUATION_COST_UNKNOWN", "Unknown provider outcome cannot assert a final actual charge")
            usage = result["usage"]
            require(usage["cached_input_tokens"] is None or usage["input_tokens"] is not None
                    and usage["cached_input_tokens"] <= usage["input_tokens"],
                    "INVALID_EVALUATION_INPUT", "Cached input usage exceeds input usage")
            receipt = result["provider_receipt"]
            require(receipt is None or receipt not in provider_receipts or provider_receipts[receipt] == aid,
                    "EVALUATION_DUPLICATE_PROVIDER_CALL", "One provider receipt cannot be counted as multiple attempts")
            if receipt is not None:
                provider_receipts[receipt] = aid
            # A provider may overrun its estimate. Keep that evidence and flag
            # the resulting cap breach; never discard an expensive failure.
            attempt["result"] = deepcopy(result)
            attempt["results"].append(deepcopy(event))
        elif kind == "CANDIDATE_DECISION":
            attempt = attempts.get(event["attempt_id"])
            candidate = attempt.get("result", {}).get("candidate") if attempt else None
            require(candidate is not None and event["candidate_sha256"] == candidate["text_ref"]["sha256"],
                    "EVALUATION_CANDIDATE_MISSING", "Decision does not identify the retained original candidate")
            decisions[event["attempt_id"]] = deepcopy(event)
        elif kind == "HUMAN_INTERVENTION":
            require(event["attempt_id"] is None or event["attempt_id"] in attempts,
                    "EVALUATION_ATTEMPT_CONFLICT", "Intervention refers to an unknown attempt")
            interventions.append(deepcopy(event))
        elif kind == "GOLD_REGISTERED":
            require(event["world_ref"] == plan["controls"]["world_ref"], "EVALUATION_GOLD_MISMATCH", "Gold belongs to a different world cut")
            require(all(g["gold_id"] != event["gold_id"] for g in gold), "EVALUATION_GOLD_MISMATCH", "Gold identity must be immutable")
            require(event["purpose"] != "LITERARY_READING" or event["source_kind"] in {"EXTERNAL_HUMAN_ANNOTATION", "ENGINEERING_SPECIFICATION"},
                    "EVALUATION_GOLD_MISMATCH", "Events and specifications are not a human literary assessment")
            gold.append(deepcopy(event))
        elif kind == "ATTEMPT_START_REJECTED":
            rejections.append(deepcopy(event))
        elif kind == "BLIND_PACKET_CREATED":
            selected = [aid for aid, decision in decisions.items() if decision["disposition"] == "SELECTED"]
            require(len(selected) == 2 and set(selected) == set(event["selected_attempt_ids"])
                    and len({attempts[aid]["condition_id"] for aid in selected}) == 2,
                    "EVALUATION_PAIR_INCOMPLETE", "Blind packet must retain exactly one selected candidate per condition")
            require(all(attempts[aid].get("result", {}).get("outcome") == "SUCCEEDED" for aid in selected),
                    "EVALUATION_PAIR_INCOMPLETE", "Unresolved or failed attempts cannot become a completed blind pair")
            packets.append(deepcopy(event))
        elif kind == "HUMAN_READING_RECORDED":
            require(any(p["packet_ref"]["sha256"] == event["blind_packet_sha256"] for p in packets),
                    "EVALUATION_READING_UNBOUND", "Reading declaration must identify an actually prepared blind packet")
            readings.append(deepcopy(event))
    return {"attempts": attempts, "decisions": decisions, "gold": gold, "interventions": interventions,
            "readings": readings, "rejected_starts": rejections, "blind_packets": packets}


def condition_accounting(plan, state):
    result = []
    budget = plan["controls"]["budget"]
    for condition in plan["conditions"]:
        attempts = [a for a in state["attempts"].values() if a["condition_id"] == condition["condition_id"]]
        charged_tokens = sum(_charge(a)[0] for a in attempts)
        charged_cost = sum((_charge(a)[1] for a in attempts), Decimal(0))
        final = [a.get("result") for a in attempts]
        known_cost = sum((Decimal(r["actual_cost_usd"]) for r in final if r and r["actual_cost_usd"] is not None), Decimal(0))
        unknown_cost = [a for a in attempts if a.get("result") is None or a["result"]["actual_cost_usd"] is None]
        known_tokens = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in final
                           if r and r["usage"]["input_tokens"] is not None and r["usage"]["output_tokens"] is not None)
        unknown_usage = sum(r is None or r["usage"]["input_tokens"] is None or r["usage"]["output_tokens"] is None for r in final)
        result.append({"condition_id": condition["condition_id"], "started_attempts": len(attempts),
            "outcomes": {name: sum((r["outcome"] if r else "IN_FLIGHT") == name for r in final)
                         for name in ("SUCCEEDED", "FAILED", "UNKNOWN", "CANCELLED", "BLOCKED", "IN_FLIGHT")},
            "known_cost_usd": str(known_cost), "total_actual_cost_usd": None if unknown_cost else str(known_cost),
            "unknown_cost_attempts": len(unknown_cost), "unknown_cost_reserved_usd": str(sum((Decimal(a["reserved_cost_usd"]) for a in unknown_cost), Decimal(0))),
            "known_tokens": known_tokens, "total_actual_tokens": None if unknown_usage else known_tokens,
            "charged_or_reserved_tokens": charged_tokens, "charged_or_reserved_usd": str(charged_cost),
            "cap_breached": charged_tokens > budget["max_total_tokens"] or charged_cost > Decimal(budget["max_cost_usd"])
                or len(attempts) > budget["max_calls"]})
    return result
