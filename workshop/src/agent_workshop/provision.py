"""Offline, trusted operator configuration. It cannot issue any NovelFactory grants."""

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb

from .auth import Denied, connect
from .organization import IDENTIFIER, NF_ROLES, employee, lock_org, schema
from .delegation import MAX_MONEY, UUID_FIELD, committed_budget

SLOT = schema(
    {
        "nf_instance_id": UUID_FIELD,
        "work_id": UUID_FIELD,
        "branch_id": UUID_FIELD,
        "actor_binding_ref": {"oneOf": [UUID_FIELD, {"type": "null"}]},
        "role_id": IDENTIFIER,
        "task_template_id": IDENTIFIER,
        "scope_ref": IDENTIFIER,
        "generation": {"const": 0},
        "grant_epoch": {"type": "integer", "minimum": 1},
        "max_amount": MAX_MONEY,
        "token_limit": {"type": "integer", "minimum": 1, "maximum": 10000000},
        "currency": {"enum": ["USD", "CNY"]},
    }
)


def allow_slot(conn, org, installation, slot, target, policy):
    organization = lock_org(conn, org, write=True)
    if organization["owner_installation_id"] != installation:
        raise Denied("INVALID_SLOT")
    e = employee(conn, org, target)
    instance = conn.execute(
        "SELECT * FROM aw_installations WHERE installation_id=%s AND enabled FOR SHARE",
        (installation,),
    ).fetchone()
    if (
        not Draft202012Validator(SLOT, format_checker=FormatChecker()).is_valid(policy)
        or not e
        or e["status"] != "ACTIVE"
        or policy["role_id"] not in NF_ROLES[e["role_id"]]
        or not instance
        or instance["nf_instance_id"] != policy["nf_instance_id"]
    ):
        raise Denied("INVALID_SLOT")
    conn.execute(
        """INSERT INTO aw_delegation_slots
        (org_id,slot_id,installation_id,employee_id,employment_revision,config)
        VALUES (%s,%s,%s,%s,%s,%s)""",
        (org, slot, installation, target, e["employment_revision"], Jsonb(policy)),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)
    budget = sub.add_parser("budget")
    budget.add_argument("org")
    budget.add_argument("currency", choices=["USD", "CNY"])
    budget.add_argument("limit", type=Decimal)
    slot = sub.add_parser("allow-slot")
    for name in ("org", "installation", "slot", "employee", "policy_file"):
        slot.add_argument(name)
    args = parser.parse_args()
    with connect(os.environ["AW_DATABASE_URL"]) as conn:
        if args.kind == "budget":
            if not args.limit.is_finite() or args.limit < 0:
                parser.error("limit must be finite and nonnegative")
            account = conn.execute(
                """INSERT INTO aw_budget_accounts VALUES (%s,%s,%s) ON CONFLICT(org_id)
                         DO UPDATE SET limit_amount=excluded.limit_amount
                         WHERE aw_budget_accounts.currency=excluded.currency RETURNING limit_amount""",
                (args.org, args.limit, args.currency),
            ).fetchone()
            if account is None:
                raise Denied("CURRENCY_MISMATCH")
            # The upsert owns the same account row lock as delegate(). Read
            # commitments in a new statement after any earlier reservation commits.
            # A rejected limit rolls back with this CLI transaction.
            if account["limit_amount"] < committed_budget(conn, args.org):
                raise Denied("BUDGET_BELOW_COMMITTED")
        else:
            allow_slot(
                conn,
                args.org,
                args.installation,
                args.slot,
                args.employee,
                json.loads(Path(args.policy_file).read_text()),
            )
