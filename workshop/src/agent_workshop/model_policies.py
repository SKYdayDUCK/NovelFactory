"""Manager-only scoped policy history. Reading or publishing here never initiates inference."""
from psycopg.types.json import Jsonb

from .auth import Denied
from .organization import require_employee, schema

_ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"}
_UUID = {"type": "string", "format": "uuid"}
_LEVELS = (("task_id",), ("instance_id", "task_kind"), ("instance_id",), ("role_id", "task_kind"),
           ("role_id",), ("task_kind",), ("branch_id",), ("work_id",), ("org_id",), ("installation_id",), ())
_RULE = schema({"id": _ID, "when": {"oneOf": [schema(dict.fromkeys(fields, _ID)) for fields in _LEVELS]},
                "profile": _ID})
_RULES = {"type": "array", "minItems": 1, "maxItems": 256, "items": _RULE}
_SCOPE = {"work_id": _UUID, "branch_id": _UUID}


def _manager(conn, session):
    if require_employee(conn, session)["role_id"] != "workshop_manager":
        raise Denied("VIEW_DENIED")


def _read(conn, session, work, branch):
    return conn.execute("""
        SELECT revision,rules FROM aw_model_policies WHERE org_id=%s AND installation_id=%s
          AND work_id=%s AND branch_id=%s ORDER BY revision DESC LIMIT 1""",
        (session.org_id, session.installation_id, work, branch)).fetchone()


def publish(conn, session, work_id, branch_id, expected_revision, rules):
    from .organization import lock_org
    lock_org(conn, session.org_id, write=True)
    _manager(conn, session)
    selectors = {tuple(sorted(r["when"].items())) for r in rules}
    if () not in selectors or len(selectors) != len(rules) or len({r["id"] for r in rules}) != len(rules):
        raise Denied("INVALID_ROUTING_RULES")
    old = _read(conn, session, work_id, branch_id)
    current = old["revision"] if old else 0
    if not (old and current == expected_revision + 1 and old["rules"] == rules):
        if expected_revision != current:
            raise Denied("STALE_REVISION")
        conn.execute("""
            INSERT INTO aw_model_policies(org_id,installation_id,work_id,branch_id,revision,rules,author_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (session.org_id, session.installation_id,
                         work_id, branch_id, current + 1, Jsonb(rules), session.employee_id))
    return get(conn, session, work_id, branch_id)


def get(conn, session, work_id, branch_id):
    _manager(conn, session)
    row = _read(conn, session, work_id, branch_id)
    if not row:
        raise Denied("POLICY_UNAVAILABLE")
    return {"org_id": session.org_id, "installation_id": session.installation_id,
            "work_id": work_id, "branch_id": branch_id, **row}


TOOLS = {
    "aw_model_policy_publish": ("Publish a scoped policy revision for authorized NF adoption.",
        schema({**_SCOPE, "expected_revision": {"type": "integer", "minimum": 0}, "rules": _RULES}), publish),
    "aw_model_policy_get": ("Export the current manager-approved model policy for this branch.",
        schema(_SCOPE), get),
}
