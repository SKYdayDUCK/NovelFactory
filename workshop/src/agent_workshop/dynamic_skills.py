"""Recheck a creative Skill as the authenticated employee, before and after NF task binding."""

from .auth import Denied
from .delegation import route
from .organization import IDENTIFIER, NF_ROLES, require_employee, schema
from .recovery import frozen
from .reviews_skills import _skill, freeze


_ID = {"type": "string", "format": "uuid"}
_NF = schema(
    {
        "installation_id": _ID,
        "org_id": _ID,
        "nf_instance_id": _ID,
        "work_id": _ID,
        "branch_id": _ID,
        "role_id": IDENTIFIER,
        "task_kind": IDENTIFIER,
        "installation_epoch": {"type": "integer", "minimum": 1},
    }
)
_TASK = schema(
    {
        "task_id": _ID,
        "generation": {"type": "integer", "minimum": 0},
        "operation_id": _ID,
        "receipt_ref": _ID,
        "scope_ref": {"type": "string", "minLength": 1, "maxLength": 128},
        "task_template_id": {"type": "string", "minLength": 1, "maxLength": 128},
    }
)


def _check(value):
    if not value:
        raise Denied("DYNAMIC_SKILL_DENIED")


def attest(conn, session, binding_id, nf, task=None, freeze_task=False):
    viewer = require_employee(conn, session)
    _check(
        (session.org_id, session.installation_id) == (nf["org_id"], nf["installation_id"])
        and nf["role_id"] in NF_ROLES[viewer["role_id"]]
        and not frozen(conn, session.installation_id)
    )
    current = conn.execute(
        """
        SELECT s.mode, s.content_sha, b.expires_at, i.nf_instance_id
        FROM aw_skill_bindings b JOIN aw_skill_sources s USING(org_id,skill_id,source_revision)
        JOIN aw_bindings a ON a.token_digest=%s JOIN aw_installations i USING(installation_id)
        WHERE b.org_id=%s AND b.binding_id=%s AND s.namespace='creative' AND i.enabled AND a.org_member
          AND a.org_id=b.org_id AND a.installation_id=%s AND a.employee_id=%s AND NOT a.revoked
          AND a.expires_at>clock_timestamp() AND s.content_sha=b.content_sha
        """,
        (session.binding_digest, session.org_id, binding_id, session.installation_id, session.employee_id),
    ).fetchone()
    _check(current and current["nf_instance_id"] == nf["nf_instance_id"])
    skill = _skill(conn, session, binding_id, current["mode"])
    caller = conn.execute(
        "SELECT expires_at FROM aw_bindings WHERE token_digest=%s", (session.binding_digest,)
    ).fetchone()
    expiry = min(current["expires_at"], caller["expires_at"])
    if task:
        row = conn.execute(
            """
            SELECT d.payload,d.sender_id,r.generation,r.task_state,b.expires_at
            FROM aw_delegations d JOIN aw_delegation_slots s USING(org_id,installation_id,slot_id)
            JOIN aw_remote_receipts r USING(org_id,operation_id)
            JOIN aw_bindings b ON b.token_digest=d.binding_digest AND b.installation_id=d.installation_id
            JOIN aw_employees se ON (se.org_id,se.employee_id,se.employment_revision)=(b.org_id,b.employee_id,b.employment_revision)
            WHERE d.org_id=%s AND d.installation_id=%s AND d.operation_id=%s AND r.task_id=%s AND r.receipt_ref=%s
              AND d.state='BOUND' AND s.enabled AND s.employee_id=%s AND s.employment_revision=%s
              AND (d.owner_id,d.owner_revision)=(s.employee_id,s.employment_revision)
              AND (b.org_id,b.employee_id,b.employment_revision)=(d.org_id,d.sender_id,d.sender_revision)
              AND b.org_member AND NOT b.revoked AND b.expires_at>clock_timestamp() AND se.status='ACTIVE'
              AND b.grant_epoch=(d.payload->>'grant_epoch')::bigint
            """,
            (
                session.org_id,
                session.installation_id,
                task["operation_id"],
                task["task_id"],
                task["receipt_ref"],
                session.employee_id,
                session.employment_revision,
            ),
        ).fetchone()
        expected = {
            "nf_instance_id": nf["nf_instance_id"],
            "work_id": nf["work_id"],
            "branch_id": nf["branch_id"],
            "scope_ref": task["scope_ref"],
            "role_id": nf["role_id"],
            "task_template_id": task["task_template_id"],
            "grant_epoch": nf["installation_epoch"],
        }
        # NF's first claim advances READY's generation before Workshop's next periodic lookup.
        # Only that single transition can proceed against its immutable, already verified NF receipt.
        _check(
            row
            and all(row["payload"].get(k) == v for k, v in expected.items())
            and row["task_state"] not in {"FAILED", "CANCELLED", "ACCEPTED"}
            and (
                row["generation"] == task["generation"]
                or (row["task_state"] == "READY" and row["generation"] + 1 == task["generation"])
            )
            and skill["mode"] == task["task_template_id"]
            and route(conn, session.org_id, row["sender_id"], viewer["employee_id"], viewer["scope_ref"])
        )
        expiry = min(expiry, row["expires_at"])
        if freeze_task:
            snapshot = freeze(conn, session, binding_id, current["mode"], task["task_id"])
            _check(snapshot["epoch"] == skill["epoch"] and snapshot["content_sha"] == skill["content_sha"])
    else:
        _check(not freeze_task)
    binding = {
        "org_id": session.org_id,
        "installation_id": session.installation_id,
        "employee_id": session.employee_id,
        "employment_revision": session.employment_revision,
        "grant_epoch": session.grant_epoch,
        "binding_id": binding_id,
        **{k: skill[k] for k in ("skill_id", "source_revision", "content_sha")},
    }
    return {
        "binding": binding,
        "nf": nf,
        "task": task,
        "frozen": freeze_task,
        "skill_epoch": skill["epoch"],
        "expires_at": expiry.isoformat(),
        "skill_mode": skill["mode"],
    }


TOOLS = {
    "aw_dynamic_skill_attest": (
        "Verify the current employee and creative Skill and optionally freeze it to the exact delegated NF task.",
        schema(
            {"binding_id": _ID, "nf": _NF, "task": _TASK, "freeze_task": {"type": "boolean"}},
            ["binding_id", "nf", "freeze_task"],
        ),
        attest,
    )
}
