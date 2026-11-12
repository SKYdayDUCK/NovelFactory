"""Deterministic resolution of a controller-authored policy. No credentials or I/O."""
import hashlib
import json
import re

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
# A branch is more specific than a work and an organization than its installation.
LEVELS = (("task_id",), ("instance_id", "task_kind"), ("instance_id",),
          ("role_id", "task_kind"), ("role_id",), ("task_kind",),
          ("branch_id",), ("work_id",), ("org_id",), ("installation_id",), ())
SCOPE = ("installation_id", "org_id", "work_id", "branch_id")


class RoutingError(ValueError):
    pass


def digest(document):
    wire = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()
    return hashlib.sha256(wire).hexdigest()


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def validate_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 256:
        raise RoutingError("INVALID_ROUTING_RULES")
    ids, selectors = set(), set()
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {"id", "when", "profile"}
                or not identifier(rule["id"]) or not identifier(rule["profile"])):
            raise RoutingError("INVALID_ROUTING_RULE")
        match = rule["when"]
        if (not isinstance(match, dict) or set(match) not in [set(level) for level in LEVELS]
                or not all(identifier(value) for value in match.values())):
            raise RoutingError("INVALID_ROUTING_SELECTOR")
        selector = tuple(sorted(match.items()))
        if rule["id"] in ids or selector in selectors:
            raise RoutingError("AMBIGUOUS_ROUTING_RULE")
        ids.add(rule["id"])
        selectors.add(selector)
    if () not in selectors:
        raise RoutingError("ROUTING_DEFAULT_REQUIRED")


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {*SCOPE, "revision", "rules"}
            or not all(identifier(policy[key]) for key in SCOPE)
            or type(policy["revision"]) is not int or policy["revision"] < 1):
        raise RoutingError("INVALID_ROUTING_POLICY")
    validate_rules(policy["rules"])


def resolve(policy, actor, *, override=None):
    validate_policy(policy)
    subject = {**actor, **{k: policy[k] for k in SCOPE}}
    if override is not None:
        if not identifier(override):
            raise RoutingError("INVALID_PROFILE")
        profile, rule_id = override, "@task-override"
    else:
        matches = [r for r in policy["rules"]
                   if all(subject.get(k) == v for k, v in r["when"].items())]
        rule = min(matches, key=lambda r: next(i for i, keys in enumerate(LEVELS)
                                               if set(keys) == set(r["when"])))
        profile, rule_id = rule["profile"], rule["id"]
    return {"profile_id": profile, "matched_rule": rule_id, "revision": policy["revision"],
            "policy_sha256": digest(policy)}
