"""PostgreSQL organization authority. All handlers run within the caller's transaction."""

from hashlib import sha256
import json
import re

from psycopg.types.json import Jsonb

from .auth import Denied
from .roster_projection import project_employees

IDENTIFIER = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,96}$"}
POSITIVE = {"type": "integer", "minimum": 1}
PRESETS = ("Minimal", "Standard", "Long-form", "Research-Audit")
NF_ROLES = {
    "workshop_manager": (),
    "author": (),
    "review_lead": (),
    "maintenance": (),
    "screenwriter": ("volume_director", "writer"),
    "chief_editor": ("chief_editor",),
    "volume_director": ("volume_director",),
    "character_actor": ("character_actor",),
    "world_designer": ("world_designer",),
    "environment_director": ("environment_director",),
    "adjudication_advisor": ("adjudication_advisor",),
    "writer": ("writer",),
    "evidence_maintainer": ("evidence_maintainer",),
    "reviewer": ("reviewer",),
    "literary_editor": ("literary_editor",),
}
SUPERVISORS = {
    "author": {"workshop_manager"},
    "screenwriter": {"workshop_manager"},
    "chief_editor": {"workshop_manager"},
    "volume_director": {"chief_editor"},
    "character_actor": {"volume_director"},
    "review_lead": {"workshop_manager"},
    "reviewer": {"review_lead"},
    "writer": {"volume_director", "chief_editor"},
    "maintenance": {"workshop_manager"},
    "world_designer": {"chief_editor"},
    "environment_director": {"volume_director"},
    "adjudication_advisor": {"chief_editor"},
    "evidence_maintainer": {"volume_director", "chief_editor"},
    "literary_editor": {"chief_editor"},
}
DOWN = {
    ("workshop_manager", "author"): ("REPORT", "REQUEST_DECISION"),
    ("workshop_manager", "screenwriter"): ("ASSIGN", "REQUEST_REVISION"),
    ("workshop_manager", "chief_editor"): ("ASSIGN", "PAUSE", "REQUEST_REVISION"),
    ("workshop_manager", "review_lead"): ("ASSIGN_REVIEW",),
    ("chief_editor", "volume_director"): ("ASSIGN", "PAUSE", "REQUEST_REVISION"),
    ("volume_director", "character_actor"): ("ASSIGN", "PLAN_NOTICE"),
    ("review_lead", "reviewer"): ("ASSIGN_REVIEW",),
}
UP = {
    "author": ("GOAL", "REQUEST_REVISION"),
    "volume_director": ("REPORT", "ASK", "REQUEST_DECISION"),
    "character_actor": ("REPORT", "ASK", "REQUEST_RESOURCE"),
}


def schema(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields if required is None else required),
        "additionalProperties": False,
    }


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER["pattern"], value):
        raise Denied("INVALID_IDENTIFIER")
    return value


def _preset(name, manager):
    if name not in PRESETS:
        raise Denied("UNKNOWN_PRESET")
    slots = [
        (manager, "workshop_manager", None, "control"),
        ("author", "author", manager, "author"),
        ("script", "screenwriter", manager, "production"),
        ("chief", "chief_editor", manager, "production"),
        ("volume", "volume_director", "chief", "production"),
        *((f"actor-{n}", "character_actor", "volume", "actors") for n in range(1, 4)),
    ]
    if name != "Minimal":
        slots += [
            ("writer", "writer", "volume", "production"),
            ("review-lead", "review_lead", manager, "audit"),
            ("reviewer", "reviewer", "review-lead", "audit"),
        ]
    if name == "Long-form":
        slots += [
            ("memory", "evidence_maintainer", "chief", "continuity"),
            ("editor", "literary_editor", "chief", "continuity"),
        ]
    if name == "Research-Audit":
        slots += [
            ("world", "world_designer", "chief", "research"),
            ("ops", "maintenance", manager, "ops"),
            ("stats", "reviewer", "review-lead", "audit"),
        ]
    if len({row[0] for row in slots}) != len(slots):
        raise Denied("RESERVED_SLOT")
    return slots


def lock_org(conn, org_id, *, write=False):
    suffix = " FOR UPDATE" if write else " FOR SHARE"
    row = conn.execute(
        "SELECT * FROM aw_organizations WHERE org_id=%s" + suffix, (org_id,)
    ).fetchone()
    if not row:
        raise Denied("ORGANIZATION_UNAVAILABLE")
    return row


def employee(conn, org_id, employee_id):
    return conn.execute(
        "SELECT * FROM aw_employees WHERE org_id=%s AND employee_id=%s", (org_id, employee_id)
    ).fetchone()


def require_employee(conn, session):
    # Writers hold this lock exclusively through commit, including tenure and route changes.
    org = lock_org(conn, session.org_id)
    if not session.org_member or org["owner_installation_id"] != session.installation_id:
        raise Denied("VIEW_DENIED")
    row = employee(conn, session.org_id, session.employee_id)
    if (
        not row
        or row["status"] != "ACTIVE"
        or row["employment_revision"] != session.employment_revision
    ):
        raise Denied("STALE_EMPLOYMENT")
    return row


def _insert_employee(conn, org, revision, emp, role, supervisor, team, scope, *,
                     display_name=None, slot_id=None):
    conn.execute(
        """INSERT INTO aw_employees
        (org_id,employee_id,employment_revision,role_id,supervisor_id,team_id,scope_ref,status,
         display_name,slot_id)
        VALUES (%s,%s,1,%s,%s,%s,%s,'ACTIVE',%s,%s)""",
        (org, emp, role, supervisor, team, scope, display_name or emp, slot_id or emp),
    )
    _snapshot(conn, org, revision, emp)


def _snapshot(conn, org, revision, emp):
    conn.execute(
        """INSERT INTO aw_employment_versions
        (org_id,employee_id,employment_revision,org_revision,role_id,team_id,supervisor_id,
         scope_ref,status,display_name,slot_id)
        SELECT org_id,employee_id,employment_revision,%s,role_id,team_id,supervisor_id,
               scope_ref,status,display_name,slot_id
        FROM aw_employees WHERE org_id=%s AND employee_id=%s""",
        (revision, org, emp),
    )


def _graph(conn, org, revision):
    rows = conn.execute(
        "SELECT * FROM aw_employees WHERE org_id=%s AND status='ACTIVE'", (org,)
    ).fetchall()
    agents = {r["employee_id"]: r for r in rows}
    edges = set()

    def add(src, dst, kinds):
        if src["scope_ref"] == dst["scope_ref"]:
            for kind in kinds:
                edges.add(
                    (org, revision, src["employee_id"], dst["employee_id"], kind, src["scope_ref"])
                )

    for row in rows:
        if row["supervisor_id"]:
            sup = agents[row["supervisor_id"]]
            add(sup, row, DOWN.get((sup["role_id"], row["role_id"]), ("ASSIGN",)))
            add(
                row,
                sup,
                (*UP.get(row["role_id"], ("REPORT", "ASK")), "EXCEPTION", "REQUEST_REVIEW"),
            )
        if row["role_id"] == "screenwriter":
            for other in rows:
                if (
                    other["role_id"] == "chief_editor"
                    and other["supervisor_id"] == row["supervisor_id"]
                ):
                    add(row, other, ("PROPOSE_SCRIPT",))
                    add(other, row, ("REQUEST_REVISION", "REPORT"))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO aw_edges VALUES (%s,%s,%s,%s,%s,%s)", sorted(edges))


def provisioning_installation(conn, installation_id):
    if installation_id is None:
        rows = conn.execute(
            "SELECT installation_id FROM aw_installations WHERE enabled ORDER BY installation_id LIMIT 2 FOR SHARE"
        ).fetchall()
        if len(rows) != 1:
            raise Denied("INSTALLATION_REQUIRED")
        return rows[0]["installation_id"]
    if not conn.execute(
        "SELECT installation_id FROM aw_installations WHERE installation_id=%s AND enabled FOR SHARE",
        (installation_id,),
    ).fetchone():
        raise Denied("INSTALLATION_UNAVAILABLE")
    return installation_id


def claim_organization(conn, org_id, installation_id):
    """A trusted operator assigns ownership to a pre-upgrade directory after verifying it."""
    owner = provisioning_installation(conn, installation_id)
    org = lock_org(conn, org_id, write=True)
    if org["owner_installation_id"] not in (None, owner):
        raise Denied("ORGANIZATION_UNAVAILABLE")
    conn.execute("UPDATE aw_organizations SET owner_installation_id=%s WHERE org_id=%s", (owner, org_id))


def bootstrap(conn, org_id, preset="Minimal", manager_id="manager", scope_ref="organization", *, installation_id=None):
    """Installation-admin CLI operation. Repeated provisioning must match the original preset."""
    for key in (org_id, manager_id, scope_ref):
        _id(key)
    owner = provisioning_installation(conn, installation_id)
    slots = _preset(preset, manager_id)
    digest = fingerprint(
        [preset, slots, NF_ROLES, sorted((list(k), v) for k, v in DOWN.items()), UP]
    )
    new = conn.execute(
        """INSERT INTO aw_organizations (org_id,active_revision,preset,preset_sha,scope_ref,owner_installation_id)
        VALUES (%s,1,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING org_id""",
        (org_id, preset, digest, scope_ref, owner),
    ).fetchone()
    org = lock_org(conn, org_id, write=True)
    if org["owner_installation_id"] != owner:
        raise Denied("ORGANIZATION_UNAVAILABLE")
    if not new:
        initial = conn.execute(
            "SELECT preset_sha FROM aw_org_revisions WHERE org_id=%s AND revision=1", (org_id,)
        ).fetchone()
        if (initial["preset_sha"], org["scope_ref"]) != (digest, scope_ref):
            raise Denied("BOOTSTRAP_MISMATCH")
        return {"org_id": org_id, "revision": 1, "preset_sha": digest}
    conn.execute(
        """INSERT INTO aw_org_revisions
        (org_id,revision,preset,preset_sha,changed_by,change_kind)
        VALUES (%s,1,%s,%s,%s,'BOOTSTRAP')""",
        (org_id, preset, digest, manager_id),
    )
    for emp, role, supervisor, team in slots:
        _insert_employee(conn, org_id, 1, emp, role, supervisor, team, scope_ref)
    _graph(conn, org_id, 1)
    return {"org_id": org_id, "revision": 1, "preset_sha": digest}


def capacity(org, total):
    if org.get("max_active_employees") is not None and total > org["max_active_employees"]:
        raise Denied("ROSTER_LIMIT_EXCEEDED")


def scope_allowed(org, role, team, scope):
    if scope != org["scope_ref"]:
        raise Denied("ROSTER_SCOPE_DENIED")
    policy = org.get("team_policy")
    if policy is not None and team not in policy.get(role, []):
        raise Denied("ROSTER_SCOPE_DENIED")


def _change(conn, session, action, expected_revision, operation_id, employee_id, **fields):
    org_id = session.org_id
    org = lock_org(conn, org_id, write=True)
    manager = require_employee(conn, session)
    if manager["role_id"] != "workshop_manager":
        raise Denied("MANAGER_REQUIRED")
    _id(operation_id)
    _id(employee_id)
    digest = fingerprint([action, expected_revision, employee_id, fields])
    key = (org_id, session.employee_id, session.employment_revision, operation_id)
    old = conn.execute(
        """SELECT request_sha,receipt FROM aw_org_operations WHERE
        (org_id,employee_id,employment_revision,operation_id)=(%s,%s,%s,%s)""",
        key,
    ).fetchone()
    if old:
        if old["request_sha"] != digest:
            raise Denied("IDEMPOTENCY_MISMATCH")
        return old["receipt"]
    if expected_revision != org["active_revision"]:
        raise Denied("STALE_ORG_REVISION")
    newrev = expected_revision + 1
    row = employee(conn, org_id, employee_id)
    if action == "HIRE":
        if row:
            raise Denied("EMPLOYEE_ALREADY_REGISTERED")
    elif not row or row["status"] == "ENDED" or row["role_id"] in ("workshop_manager", "author"):
        raise Denied("INVALID_EMPLOYMENT_TRANSITION")
    if action in ("HIRE", "TRANSFER"):
        role, sup_id, team = fields["role_id"], _id(fields["supervisor_id"]), _id(fields["team_id"])
        if action == "HIRE" or row["status"] != "ACTIVE":
            current = conn.execute(
                "SELECT count(*) AS n FROM aw_employees WHERE org_id=%s AND status='ACTIVE'",
                (org_id,),
            ).fetchone()["n"]
            capacity(org, current + 1)
        scope_allowed(org, role, team, org["scope_ref"])
        sup = employee(conn, org_id, sup_id)
        if not sup or sup["status"] != "ACTIVE" or sup_id == employee_id:
            raise Denied("INVALID_SUPERVISOR")
        if sup["role_id"] not in SUPERVISORS.get(role, ()) or role == "author":
            raise Denied("INCOMPATIBLE_ROLE")
        if (
            row
            and row["role_id"] != role
            and conn.execute(
                "SELECT 1 FROM aw_employees WHERE org_id=%s AND supervisor_id=%s AND status='ACTIVE' LIMIT 1",
                (org_id, employee_id),
            ).fetchone()
        ):
            raise Denied("REASSIGN_REPORTS_FIRST")
        ancestor = sup
        while ancestor:
            if ancestor["employee_id"] == employee_id:
                raise Denied("SUPERVISOR_CYCLE")
            ancestor = (
                employee(conn, org_id, ancestor["supervisor_id"])
                if ancestor["supervisor_id"]
                else None
            )
        slot_id = _id(fields.get("slot_id", row["slot_id"] if row else employee_id))
        display_name = fields.get("display_name", row["display_name"] if row else employee_id)
        if (not isinstance(display_name, str) or not 1 <= len(display_name) <= 128
                or not display_name.strip() or not display_name.isprintable()):
            raise Denied("INVALID_DISPLAY_NAME")
        if conn.execute(
            """SELECT 1 FROM aw_employees WHERE org_id=%s AND slot_id=%s
               AND status='ACTIVE' AND employee_id<>%s""",
            (org_id, slot_id, employee_id),
        ).fetchone():
            raise Denied("SLOT_OCCUPIED")
        if action == "HIRE":
            _insert_employee(
                conn, org_id, newrev, employee_id, role, sup_id, team, org["scope_ref"],
                display_name=display_name, slot_id=slot_id,
            )
        else:
            conn.execute(
                """UPDATE aw_employees SET employment_revision=employment_revision+1,
                role_id=%s,supervisor_id=%s,team_id=%s,status='ACTIVE',display_name=%s,slot_id=%s
                WHERE org_id=%s AND employee_id=%s""",
                (role, sup_id, team, display_name, slot_id, org_id, employee_id),
            )
    else:
        if (action == "SUSPEND" and row["status"] != "ACTIVE") or conn.execute(
            "SELECT 1 FROM aw_employees WHERE org_id=%s AND supervisor_id=%s AND status='ACTIVE' LIMIT 1",
            (org_id, employee_id),
        ).fetchone():
            raise Denied("INVALID_EMPLOYMENT_TRANSITION")
        status = "SUSPENDED" if action == "SUSPEND" else "ENDED"
        conn.execute(
            """UPDATE aw_employees SET employment_revision=employment_revision+1,status=%s
            WHERE org_id=%s AND employee_id=%s""",
            (status, org_id, employee_id),
        )
    if action != "HIRE":
        _snapshot(conn, org_id, newrev, employee_id)
    conn.execute("UPDATE aw_organizations SET active_revision=%s WHERE org_id=%s", (newrev, org_id))
    conn.execute(
        """INSERT INTO aw_org_revisions
        (org_id,revision,preset,preset_sha,changed_by,change_kind) VALUES (%s,%s,%s,%s,%s,%s)""",
        (org_id, newrev, org["preset"], org["preset_sha"], session.employee_id, action),
    )
    _graph(conn, org_id, newrev)
    receipt = {
        "org_id": org_id,
        "revision": newrev,
        "employee_id": employee_id,
        "employment_revision": employee(conn, org_id, employee_id)["employment_revision"],
    }
    conn.execute(
        "INSERT INTO aw_org_operations VALUES (%s,%s,%s,%s,%s,%s)", (*key, digest, Jsonb(receipt))
    )
    return receipt


def roster(conn, session):
    viewer = require_employee(conn, session)
    org = lock_org(conn, session.org_id)
    whole = viewer["role_id"] in ("workshop_manager", "author")
    visible = conn.execute(
        """SELECT v.employee_id,v.role_id,v.team_id,v.supervisor_id,v.employment_revision,
                  v.status,e.display_name
           FROM aw_roster_views v JOIN aw_employees e USING (org_id,employee_id)
           WHERE v.org_id=%s AND v.revision=%s AND v.audience=%s ORDER BY v.employee_id""",
        (session.org_id, org["active_revision"], session.employee_id),
    ).fetchall()
    routes = conn.execute(
        """SELECT src,dst,kind,scope_ref FROM aw_edges WHERE org_id=%s AND graph_revision=%s
        AND (%s OR src=%s OR dst=%s) ORDER BY src,dst,kind,scope_ref""",
        (session.org_id, org["active_revision"], whole, session.employee_id, session.employee_id),
    ).fetchall()
    return {
        "org_id": session.org_id,
        "revision": org["active_revision"],
        "preset": org["preset"],
        "employees": project_employees(visible, viewer, routes, NF_ROLES[viewer["role_id"]]),
        "routes": routes,
    }


def state(conn, session):
    view = roster(conn, session)
    view.pop("routes")
    for member in view["employees"]:
        member.pop("allowed_routes", None)
    return view


def _handler(action):
    def handle(conn, session, **args):
        return _change(conn, session, action, **args)

    return handle


CHANGE = {"operation_id": IDENTIFIER, "expected_revision": POSITIVE, "employee_id": IDENTIFIER}
ROLE = {
    "role_id": {"type": "string", "enum": sorted(SUPERVISORS.keys() - {"author"})},
    "supervisor_id": IDENTIFIER,
    "team_id": IDENTIFIER,
}
TOOLS = {
    "aw_roster_get": ("Read the current directory and permitted routes.", schema({}), roster),
    "aw_state_get": (
        "Read the current organization state visible to this identity.",
        schema({}),
        state,
    ),
    **{
        "aw_" + action.lower(): (
            f"{action.title()} an employee using organization revision CAS.",
            schema(
                CHANGE | ROLE | {"slot_id": IDENTIFIER, "display_name":
                                {"type": "string", "minLength": 1, "maxLength": 128}},
                required=[*CHANGE, *ROLE],
            ) if action in ("HIRE", "TRANSFER") else schema(CHANGE),
            _handler(action),
        )
        for action in ("HIRE", "TRANSFER", "SUSPEND", "END")
    },
}
