"""A stopped strategy is not a successfully tested method."""
import pytest

from novel_factory.application.quality_memory import PAUSED, verify_method


@pytest.mark.parametrize("plan", [None, {}, {"method": None}, {"method": ""}, {"method": "   "},
                                  {"method": "A method awaiting diagnosis", "decision": PAUSED}])
def test_unresolved_repair_strategy_never_reaches_experience_promotion(plan):
    # No evidence or repository is available: a paused/empty method must exit before proof reads.
    verify_method(None, None, None, None, plan, {}, None, None, None, None)
