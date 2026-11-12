from types import SimpleNamespace

import pytest

from benchmarks import p59_http_shards as driver


def test_wrong_principal_and_error_responses_never_count_as_success():
    rows = [
        {"index": 0, "status": 200, "valid": False, "_content": b'{"principal_id":"other"}'},
        {"index": 1, "status": 429, "valid": False, "_content": b"{}"},
        {"index": 2, "status": "transport_error", "valid": False},
    ]
    fixture = {"population": [{"principal": SimpleNamespace(principal_id="mine")}]}
    driver.validate(rows, fixture, "auth")
    assert rows[0]["correctness_error"] == "wrong_response"
    assert rows[1].get("correctness_error")
    assert not any(row["valid"] for row in rows)
    assert all("_content" not in row for row in rows)


def test_counter_boundaries_keep_unsent_timeouts_and_admission_failure():
    measurements = {
        "scheduled_seconds": 0.0,
        "dispatch_seconds": 0.01,
        "completed_seconds": 1,
        "valid": False,
    }
    rows = [
        {**measurements, "index": 0, "status": "timeout"},
        {
            **measurements,
            "index": 1,
            "attempt_seconds": 0.02,
            "status": 429,
            "wire_start_seconds": 0.03,
            "code": "BACKPRESSURE",
        },
    ]
    result = driver.summarize(
        rows,
        2,
        1,
        {
            "ingress": {"observed_received": 1, "observed_operation_entries": 0},
            "admission": {"admitted": 1, "timed_out": 1},
        },
        "query",
    )
    assert [
        result[key]
        for key in (
            "planned",
            "scheduled",
            "attempted",
            "headers_started",
            "received_by_server",
            "admitted_by_server",
            "http_429",
            "timeouts",
            "validated",
        )
    ] == [2, 2, 1, 1, 1, 0, 1, 1, 0]
    assert result["layer_gate"] == "NOT_MET"
    with pytest.raises(RuntimeError, match="Lost"):
        driver.summarize(rows[:1], 2, 1, {}, "query")
