"""A lifetime node allowance independent of changing technical diagnostics."""
from .kernel import require

MAX_TECHNICAL_REPAIRS = 6
EXHAUSTED = "TECHNICAL_REPAIR_BUDGET_EXHAUSTED"


def repair_count(node):
    schema = node.get("schema_repairs", [])
    facts = node.get("fact_format_repairs", [])
    require(isinstance(schema, list) and isinstance(facts, list)
            and all(isinstance(row, dict) for row in [*schema, *facts]),
            "INVALID_ARGUMENT", "Invalid technical repair history")
    # Review format reassessments amend their original row. Fact format
    # reassessments are separate rows already; do not count them twice.
    return len(schema) + sum(bool(row.get("reassessment")) for row in schema) + len(facts) + len(node.get("evidence_repairs", []))


def technical_repair_budget(node):
    count = repair_count(node)
    allowance = node.get("technical_repair_allowance", {})
    require(isinstance(allowance, dict), "INVALID_ARGUMENT", "Invalid technical repair allowance")
    baseline = allowance.get("repair_baseline", 0)
    require(type(baseline) is int and 0 <= baseline <= count,
            "INVALID_ARGUMENT", "Invalid technical repair baseline")
    return EXHAUSTED if count - baseline >= MAX_TECHNICAL_REPAIRS else None
