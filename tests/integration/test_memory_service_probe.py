"""P3-03 paired compiler-cost probe must keep actual Kernel read boundaries."""
import json

import pytest

from benchmarks.memory.service_probe import run_probe

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_real_memory_service_probe_preserves_sources_scope_and_world(kernel_env, record_testsuite_property):
    result = run_probe(kernel_env["kernel"], repeats=1)
    assert result["status"] == "PASS_ENGINEERING_ONLY"
    assert len(result["gates"]) == 9 and all(result["gates"].values())
    focused, broad = result["comparisons"]
    assert focused["gold_exact_state_correct"] and broad["gold_exact_state_correct"]
    assert focused["section_bytes"] < broad["section_bytes"]
    assert focused["sql_statements"] > 0 and broad["sql_statements"] > 0
    assert result["before"] == result["after"]
    assert result["real_model_calls"] == 0 and result["model_cost"] is None
    record_testsuite_property("p3_memory_probe", json.dumps({"gates": result["gates"],
                                                            "comparisons": result["comparisons"]}))
