import pytest

from agent_runtime.routing import RoutingError, resolve, validate_policy

pytestmark = pytest.mark.unit


def test_resolution_precedence_and_ambiguous_rejection():
    fields = {"task_id": "t", "instance_id": "a", "role_id": "r", "task_kind": "write",
              "work_id": "w", "branch_id": "b", "org_id": "o", "installation_id": "i"}
    from agent_runtime.routing import LEVELS, SCOPE
    policy = {k: fields[k] for k in SCOPE}
    rules = [{"id": f"rule-{i}", "when": {k: fields[k] for k in level}, "profile": f"profile-{i}"}
             for i, level in enumerate(LEVELS)]
    for i in range(len(rules)):
        policy.update(revision=1, rules=list(reversed(rules[i:])))
        assert resolve(policy, fields)["matched_rule"] == rules[i]["id"]
        assert resolve(policy, fields, override="direct")["profile_id"] == "direct"
    policy["rules"].append({**policy["rules"][-1], "id": "other"})
    with pytest.raises(RoutingError, match="AMBIGUOUS"):
        validate_policy(policy)
