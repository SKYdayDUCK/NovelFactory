"""Preview and atomically apply an organizational preset while retaining operator-owned staff."""

import argparse
import json
import os
from collections import Counter

from .auth import Denied, connect
from .messaging import operation
from .organization import (
    DOWN,
    IDENTIFIER,
    NF_ROLES,
    POSITIVE,
    PRESETS,
    SUPERVISORS,
    UP,
    _graph,
    _insert_employee,
    _preset,
    _snapshot,
    capacity,
    fingerprint,
    lock_org,
    require_employee,
    schema,
    scope_allowed,
)


def _plan(conn, session, target):
    org = lock_org(conn, session.org_id)
    if require_employee(conn, session)["role_id"] != "workshop_manager":
        raise Denied("MANAGER_REQUIRED")
    old_slots = _preset(org["preset"], session.employee_id)
    new_slots = _preset(target, session.employee_id)
    old = {r[0]: r for r in old_slots}
    new = {r[0]: r for r in new_slots}
    data = conn.execute(
        """SELECT e.*,coalesce(p.managed_revision,seed.employment_revision) AS managed_revision,
        coalesce(p.parked,false) AS parked FROM aw_employees e LEFT JOIN aw_preset_members p USING(org_id,employee_id)
        LEFT JOIN aw_employment_versions seed ON seed.org_id=e.org_id AND seed.employee_id=e.employee_id AND seed.org_revision=1
        WHERE e.org_id=%s""",
        (session.org_id,),
    ).fetchall()
    people = {r["employee_id"]: r for r in data}
    add, park, resume, blocked = [], [], [], []
    for key in sorted(old.keys() - new.keys()):
        row = people.get(key)
        # Independently changed employees retain their job and tenure.
        if (
            row
            and row["status"] == "ACTIVE"
            and row["employment_revision"] == row["managed_revision"]
        ):
            park.append(key)
    for key in sorted(new.keys() - old.keys()):
        row = people.get(key)
        if not row:
            add.append(key)
        elif (
            row["status"] == "SUSPENDED"
            and row["parked"]
            and row["employment_revision"] == row["managed_revision"]
        ):
            resume.append(key)
        else:
            blocked.append(key)
    for row in data:
        if (
            row["status"] == "ACTIVE"
            and row["employee_id"] not in park
            and row["supervisor_id"] in park
        ):
            blocked.append(row["employee_id"])
    # Suspensions free their slots before this transaction adds or reactivates staff.
    # A manually appointed employee keeps the slot until their own tenure ends.
    occupied = Counter(
        row["slot_id"] for row in data
        if row["status"] == "ACTIVE" and row["employee_id"] not in park
    )
    requested = {key: people[key]["slot_id"] if key in people else key for key in add + resume}
    occupied.update(requested.values())
    blocked.extend(key for key, slot in requested.items() if occupied[slot] > 1)
    for key in add + resume:
        _, role, sup, team = new[key]
        if sup not in add + resume and (
            sup not in people or people[sup]["status"] != "ACTIVE" or sup in park
            or people[sup]["role_id"] not in SUPERVISORS[role]
        ):
            blocked.append(key)
        scope_allowed(org, role, team, org["scope_ref"])
    active = sum(row["status"] == "ACTIVE" for row in data)
    projected = active - len(park) + len(add) + len(resume)
    if projected > active:
        capacity(org, projected)
    plan = {
        "org_id": session.org_id,
        "revision": org["active_revision"],
        "from": org["preset"],
        "to": target,
        "add": add,
        "suspend": park,
        "reactivate": resume,
        "blocked": sorted(set(blocked)),
    }
    return plan, new_slots, org


def preview(conn, session, preset):
    plan, _, _ = _plan(conn, session, preset)
    return {**plan, "preview_sha": fingerprint(plan)}


def apply_preset(conn, session, preset, operation_id, expected_revision, preview_sha):
    lock_org(conn, session.org_id, write=True)
    if require_employee(conn, session)["role_id"] != "workshop_manager":
        raise Denied("MANAGER_REQUIRED")
    with operation(
        conn, session, "PRESET", operation_id, [preset, expected_revision, preview_sha]
    ) as (prior, report):
        if prior:
            return report
        plan, slots, org = _plan(conn, session, preset)
        if expected_revision != org["active_revision"] or fingerprint(plan) != preview_sha:
            raise Denied("STALE_PREVIEW")
        if plan["blocked"]:
            raise Denied("PRESET_CONFLICT")
        if org["preset"] == preset:
            report.update(revision=expected_revision, preset=preset)
            return report
        rev = expected_revision + 1
        digest = fingerprint(
            [preset, slots, NF_ROLES, sorted((list(k), v) for k, v in DOWN.items()), UP]
        )
        conn.execute(
            """INSERT INTO aw_org_revisions
            (org_id,revision,preset,preset_sha,changed_by,change_kind) VALUES(%s,%s,%s,%s,%s,'PRESET_SWITCH')""",
            (session.org_id, rev, preset, digest, session.employee_id),
        )
        for key in plan["suspend"] + plan["reactivate"]:
            parked = key in plan["suspend"]
            row = conn.execute(
                """UPDATE aw_employees SET status=%s,employment_revision=employment_revision+1
                 WHERE org_id=%s AND employee_id=%s RETURNING employment_revision""",
                ("SUSPENDED" if parked else "ACTIVE", session.org_id, key),
            ).fetchone()
            _snapshot(conn, session.org_id, rev, key)
            conn.execute(
                """INSERT INTO aw_preset_members VALUES(%s,%s,%s,%s) ON CONFLICT(org_id,employee_id)
                 DO UPDATE SET managed_revision=EXCLUDED.managed_revision,parked=EXCLUDED.parked""",
                (session.org_id, key, row["employment_revision"], parked),
            )
        for key, role, sup, team in slots:
            if key in plan["add"]:
                _insert_employee(conn, session.org_id, rev, key, role, sup, team, org["scope_ref"])
                conn.execute(
                    "INSERT INTO aw_preset_members VALUES(%s,%s,1,false)", (session.org_id, key)
                )
        conn.execute(
            "UPDATE aw_organizations SET preset=%s,preset_sha=%s,active_revision=%s WHERE org_id=%s",
            (preset, digest, rev, session.org_id),
        )
        _graph(conn, session.org_id, rev)
        report.update(
            revision=rev,
            preset=preset,
            added=plan["add"],
            suspended=plan["suspend"],
            reactivated=plan["reactivate"],
        )
        return report


def configure_limits(conn, org_id, max_active_employees, max_active_delegations, role_teams):
    from psycopg.types.json import Jsonb

    org = lock_org(conn, org_id, write=True)
    if not (0 < max_active_employees <= 100000 and 0 < max_active_delegations <= 100000):
        raise Denied("INVALID_LIMIT")
    if not isinstance(role_teams, dict) or any(
        k not in NF_ROLES
        or not isinstance(v, list)
        or not v
        or any(not isinstance(t, str) or not t for t in v)
        for k, v in role_teams.items()
    ):
        raise Denied("INVALID_TEAM_POLICY")
    # Lowering an admission ceiling never cancels existing tasks, holds or employment.
    conn.execute(
        """UPDATE aw_organizations SET max_active_employees=%s,max_active_delegations=%s,team_policy=%s
        WHERE org_id=%s""",
        (max_active_employees, max_active_delegations, Jsonb(role_teams), org["org_id"]),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Set operator-owned organizational admission policy."
    )
    parser.add_argument("org")
    parser.add_argument("max_active_employees", type=int)
    parser.add_argument("max_active_delegations", type=int)
    parser.add_argument("role_teams_json")
    args = parser.parse_args()
    with open(args.role_teams_json, encoding="utf8") as f:
        role_teams = json.load(f)
    with connect(os.environ["AW_DATABASE_URL"]) as conn:
        configure_limits(
            conn, args.org, args.max_active_employees, args.max_active_delegations, role_teams
        )


TARGET = {"type": "string", "enum": list(PRESETS)}
TOOLS = {
    "aw_preset_preview": (
        "Preview roster, tenure and supervision changes.",
        schema({"preset": TARGET}),
        preview,
    ),
    "aw_preset_apply": (
        "Apply a matching preview at the current organization revision.",
        schema(
            {
                "preset": TARGET,
                "operation_id": IDENTIFIER,
                "expected_revision": POSITIVE,
                "preview_sha": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            }
        ),
        apply_preset,
    ),
}


if __name__ == "__main__":
    main()
