"""Price scoped DeepSeek runtime exports against a dated, operator-verified USD price card."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from novel_factory.application.cost_reports import estimate_usage_cost


def transport(row):
    try:
        values = [datetime.fromisoformat(row[k]) for k in ("sent_at", "received_at")]
        if all(v.utcoffset() is not None for v in values) and values[0] < values[1]:
            return tuple(v.astimezone(UTC) for v in values)
    except (KeyError, ValueError, TypeError):
        pass
    return None


def applicable_rate(card, model, span):
    if not span:
        return None, "MISSING_TRANSPORT_TIME"
    start, end = span
    if start.date() != (end - timedelta(microseconds=1)).date():
        return None, "CROSSES_DATE_BOUNDARY"
    day = card["utc_dates"].get(start.date().isoformat())
    if day is None or model not in card["models"]:
        return None, "NO_VERIFIED_PRICE"
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for lo, hi in day["peak_hours"]:
        begin, finish = midnight + timedelta(hours=lo), midnight + timedelta(hours=hi)
        if start >= begin and end <= finish:
            return card["models"][model]["peak"], "peak"
        if start < finish and end > begin:
            return None, "CROSSES_PRICE_BOUNDARY"
    return card["models"][model]["off_peak"], "off_peak"


def summarize(exports, card):
    if card["currency"] != "USD":
        raise ValueError("Expected a USD price card")
    scopes, seen, rows, endpoints = set(), set(), [], []
    groups = defaultdict(lambda: {"calls": 0, "estimated_calls": 0, "estimated_usd": Decimal(0)})
    unknown = Counter()
    unclassified_by_scope = Counter()
    for path in exports:
        tasks, calls = {}, []
        with path.open(encoding="utf8") as stream:
            scope = json.loads(next(stream))
            if scope["record"] != "scope":
                raise ValueError("Missing scope record")
            book = (scope["work_id"], scope["branch_id"])
            if book in scopes:
                raise ValueError("Supply exactly one snapshot per work and branch")
            scopes.add(book)
            for line in stream:
                item = json.loads(line)
                if item["record"] == "task_generation":
                    tasks[item["task_id"], item.get("generation")] = item
                elif item["record"] == "call":
                    calls.append(item)
        for call in calls:
            if call["call_origin"] == "UNOBSERVED":
                unclassified_by_scope[book] += 1
            if (call["call_origin"], call["adapter"], call["provider"]) != (
                "PROVIDER",
                "deepseek",
                "api.deepseek.com",
            ):
                continue
            attempt = call["attempt_id"]
            if attempt in seen:
                raise ValueError("Duplicate attempt")
            seen.add(attempt)
            task = tasks[call["task_id"], call.get("generation")]
            group = groups[(*book, task["task_kind"], task["role_id"], call["model"])]
            group["calls"] += 1
            span = transport(call)
            if span:
                endpoints.extend([(span[0], 1), (span[1], -1)])
            rate, band = applicable_rate(card, call["model"], span)
            cost, reason = estimate_usage_cost(call, rate)
            uncertainty = (reason if rate else band) if cost is None else None
            if cost is None:
                unknown[uncertainty] += 1
            else:
                group["estimated_usd"] += cost
                group["estimated_calls"] += 1
            savings = None
            if cost is not None and call["cached_input_tokens"] is not None:
                savings = (
                    (Decimal(rate["input_per_million"]) - Decimal(rate["cached_input_per_million"]))
                    * call["cached_input_tokens"]
                    / 1_000_000
                )
            rows.append(
                {
                    **{
                        k: call.get(k)
                        for k in (
                            "attempt_id",
                            "task_id",
                            "model",
                            "ledger_state",
                            "currency",
                            "input_tokens",
                            "output_tokens",
                            "cached_input_tokens",
                            "reserved_amount",
                            "actual_amount",
                            "call_reason",
                        )
                    },
                    "work_id": book[0],
                    "branch_id": book[1],
                    "stage": task["task_kind"],
                    "price_period": band,
                    "estimated_usd": cost,
                    "cache_savings_usd": savings,
                    "missing_estimate": uncertainty,
                    "invoiced_amount": call["actual_amount"]
                    if call.get("invoice_reconciled")
                    else None,
                    "transport_seconds": (span[1] - span[0]).total_seconds() if span else None,
                }
            )
    active = peak = 0
    for _, delta in sorted(endpoints):  # Ends before starts at the same instant.
        active += delta
        peak = max(peak, active)
    by_work = []
    for work, branch in sorted(scopes):
        attempts = [r for r in rows if (r["work_id"], r["branch_id"]) == (work, branch)]
        unobserved = unclassified_by_scope[work, branch]
        observed = [
            r
            for r in attempts
            if type(r["input_tokens"]) is int and type(r["output_tokens"]) is int
        ]
        priced = [r["estimated_usd"] for r in attempts if r["estimated_usd"] is not None]
        bills = defaultdict(Decimal)
        invoiced = 0
        for r in attempts:
            if r["invoiced_amount"] is not None:
                bills[r["currency"]] += Decimal(r["invoiced_amount"])
                invoiced += 1
        by_work.append(
            {
                "work_id": work,
                "branch_id": branch,
                "deepseek_attempts": len(attempts),
                "unclassified_attempts": unobserved,
                "measured_usage_calls": len(observed),
                "known_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in observed)
                if observed
                else None,
                "total_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in observed)
                if not unobserved and len(observed) == len(attempts)
                else None,
                "known_estimated_usd": sum(priced) if priced else None,
                "estimated_usd": sum(priced)
                if not unobserved and len(priced) == len(attempts)
                else None,
                "invoiced_calls": invoiced,
                "known_invoiced_by_currency": dict(bills),
                "total_invoiced_by_currency": dict(bills)
                if not unobserved and invoiced == len(attempts)
                else None,
            }
        )
    return {
        "source": card["source"],
        "by_work": by_work,
        "attempts": rows,
        "by_work_stage_model": [
            {
                "work_id": w,
                "branch_id": b,
                "stage": stage,
                "role": role,
                "model": m,
                **metrics,
                "known_estimated_usd": metrics["estimated_usd"]
                if metrics["estimated_calls"]
                else None,
                "estimated_usd": metrics["estimated_usd"]
                if metrics["estimated_calls"] == metrics["calls"]
                else None,
            }
            for (w, b, stage, role, m), metrics in sorted(groups.items())
        ],
        "missing_estimates": dict(unknown),
        "unclassified_attempts": sum(unclassified_by_scope.values()),
        "peak_observed_transports": peak if endpoints else None,
        "attempts_with_transport": len(endpoints) // 2,
        "platform_judge_cost": "NOT_OBSERVABLE",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-card", required=True, type=Path)
    parser.add_argument("exports", nargs="+", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            summarize(args.exports, json.loads(args.price_card.read_bytes())), indent=2, default=str
        )
    )
