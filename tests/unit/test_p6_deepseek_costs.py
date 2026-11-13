"""Exercise price and trace boundaries without contacting a supplier."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.p6_deepseek_costs import summarize

CARD = json.loads(
    (
        Path(__file__).resolve().parents[2] / "reports/p6/evidence/deepseek-usd-20261113.json"
    ).read_text()
)


def snapshot(path, calls):
    task = {"record": "task_generation", "task_id": "w", "task_kind": "WRITE", "role_id": "writer"}
    rows = [{"record": "scope", "work_id": path.name, "branch_id": "b"}, task]
    default = {
        "record": "call",
        "task_id": "w",
        "call_origin": "PROVIDER",
        "adapter": "deepseek",
        "provider": "api.deepseek.com",
        "model": "deepseek-flash",
        "ledger_state": "UNKNOWN",
        "input_tokens": 1000,
        "output_tokens": 120,
        "cached_input_tokens": 400,
        "actual_amount": None,
        "sent_at": "2026-11-13T02:00:00Z",
        "received_at": "2026-11-13T02:00:03Z",
    }
    for i, item in enumerate(calls):
        rows.append({**default, "attempt_id": path.name + str(i), **item})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_attempt_price_uses_observed_cache_utc_band_and_half_open_windows(tmp_path):
    first = snapshot(
        tmp_path / "a",
        [
            {},
            {
                "model": "deepseek-v4-pro",
                "sent_at": "2026-11-13T02:00:03Z",
                "received_at": "2026-11-13T02:00:08Z",
            },
            {"call_origin": "FIXTURE"},
            {"call_origin": "UNOBSERVED"},
        ],
    )
    second = snapshot(
        tmp_path / "b",
        [
            {"sent_at": "2026-11-13T07:00:00+08:00", "received_at": "2026-11-13T07:00:01+08:00"},
            {"sent_at": "2026-11-13T02:00:03Z", "received_at": "2026-11-13T02:00:07Z"},
            {"sent_at": "2026-11-13T05:00:00Z", "received_at": "2026-11-13T05:00:04Z"},
        ],
    )
    report = summarize([first, second], CARD)
    peak, pro, unpriced, overlap, off_peak = report["attempts"]
    assert peak["estimated_usd"] == Decimal("0.0003264")
    assert off_peak["estimated_usd"] == peak["estimated_usd"] / 2
    assert pro["cache_savings_usd"] == Decimal("0.0005104")
    assert peak["invoiced_amount"] is None and peak["ledger_state"] == "UNKNOWN"
    assert report["unclassified_attempts"] == 1 and len(report["attempts"]) == 5
    assert unpriced["missing_estimate"] == "NO_VERIFIED_PRICE" and unpriced["estimated_usd"] is None
    assert overlap["transport_seconds"] == 4 and report["peak_observed_transports"] == 2
    with pytest.raises(ValueError, match="snapshot"):
        summarize([first, first], CARD)


def test_unknown_usage_and_cross_boundary_are_not_priced(tmp_path):
    path = snapshot(
        tmp_path / "a",
        [
            {"received_at": None},
            {"cached_input_tokens": None},
            {"sent_at": "2026-11-13T03:59:59Z", "received_at": "2026-11-13T04:00:04Z"},
        ],
    )
    report = summarize([path], CARD)
    assert set(report["missing_estimates"]) == {
        "MISSING_TRANSPORT_TIME",
        "MISSING_OR_INVALID_CACHE_USAGE",
        "CROSSES_PRICE_BOUNDARY",
    }
    assert report["by_work_stage_model"][0]["estimated_usd"] is None


def test_per_work_partial_and_invoiced_by_actual_currency(tmp_path):
    first = snapshot(
        tmp_path / "a",
        [
            {"currency": "CNY", "actual_amount": "0.25", "invoice_reconciled": True},
            {"call_origin": "UNOBSERVED"},
        ],
    )
    second = snapshot(tmp_path / "b", [{"currency": "USD", "actual_amount": "0.13"}])
    first_work, second_work = summarize([first, second], CARD)["by_work"]
    assert first_work["known_tokens"] == 1120 and first_work["total_tokens"] is None
    assert first_work["estimated_usd"] is None and first_work["known_estimated_usd"] > 0
    assert first_work["known_invoiced_by_currency"] == {"CNY": Decimal("0.25")}
    assert (
        first_work["total_invoiced_by_currency"] is None
        and second_work["known_invoiced_by_currency"] == {}
    )
    assert second_work["total_tokens"] == 1120 and second_work["total_invoiced_by_currency"] is None


def test_half_open_call_ending_at_midnight_uses_only_prior_date(tmp_path):
    path = snapshot(
        tmp_path / "a",
        [
            {"sent_at": "2026-11-13T23:59:59.999999Z", "received_at": "2026-11-14T00:00:00Z"},
            {
                "sent_at": "2026-11-13T23:59:59.999999Z",
                "received_at": "2026-11-14T00:00:00.000001Z",
            },
        ],
    )
    prior, crossed = summarize([path], CARD)["attempts"]
    assert prior["price_period"] == "off_peak" and prior["estimated_usd"] > 0
    assert crossed["missing_estimate"] == "CROSSES_DATE_BOUNDARY"


def test_cny_rates_preserve_invoice_currency(tmp_path):
    source = Path(__file__).resolve().parents[2] / "reports/p6/evidence/deepseek-cny-20261113.json"
    path = snapshot(tmp_path / "a", [
        {"sent_at": "2026-11-13T06:13:03Z", "received_at": "2026-11-13T06:13:22Z",
         "input_tokens": 3698, "cached_input_tokens": 1408, "output_tokens": 1301,
         "currency": "USD", "actual_amount": "0.03", "invoice_reconciled": True}
    ])
    report = summarize([path], json.loads(source.read_text()))
    assert report["estimate_currency"] == "CNY"
    assert report["attempts"][0]["estimated_cny"] == Decimal("0.01504432")
    assert report["by_work"][0]["estimated_cny"] == Decimal("0.01504432")
    assert report["by_work_stage_model"][0]["known_estimated_cny"] == Decimal("0.01504432")
    assert report["by_work"][0]["total_invoiced_by_currency"] == {"USD": Decimal("0.03")}
