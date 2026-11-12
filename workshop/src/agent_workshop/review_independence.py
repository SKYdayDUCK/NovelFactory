"""Authenticate a frozen NF creative review against W4's existing receipts and subject groups."""
import hashlib
import json

from .auth import Denied
from .delegation import route
from .organization import schema
from .recovery import frozen
from .reviews_skills import _group, _independent, _require_review_team, require_role


_ID = {"type": "string", "format": "uuid"}
_SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_NUM = {"type": "integer", "minimum": 0}
_DELEGATION = schema({"installation_id": _ID, "org_id": _ID, "nf_instance_id": _ID, "epoch": _NUM,
                      "operation_id": _ID, "receipt_ref": _ID, "scope_ref": {"type": "string", "maxLength": 128}})
_PERSON = schema({"principal_id": _ID, "grant_id": _ID, "task_id": _ID, "generation": _NUM,
                  "permission_epoch": _NUM, "delegation": _DELEGATION})
_BINDING = schema({"authority": {"const": "WORKSHOP"},
                   "world_ref": schema({key: _ID for key in ("work_id", "branch_id", "world_revision_id")}),
                   "manuscript_ref": schema({"artifact_id": _ID, "artifact_revision_id": _ID, "sha256": _SHA}),
                   "producers": {"type": "array", "minItems": 1, "maxItems": 32, "items": _PERSON},
                   "reviewer": _PERSON})


def _check(ok):
    if not ok:
        raise Denied("INDEPENDENT_REVIEW_REQUIRED")


def _employee(conn, session, binding, party):
    fence = party["delegation"]
    _check((fence["org_id"], fence["installation_id"]) == (session.org_id, session.installation_id))
    row = conn.execute("""
        SELECT d.payload,d.owner_id,d.owner_revision,s.employee_id,s.employment_revision,
               i.nf_instance_id,emp.scope_ref,emp.role_id,d.sender_id,b.expires_at
        FROM aw_delegations d JOIN aw_delegation_slots s USING(org_id,slot_id,installation_id)
        JOIN aw_installations i USING(installation_id)
        JOIN aw_remote_receipts r USING(org_id,operation_id)
        JOIN aw_employees emp ON (emp.org_id,emp.employee_id,emp.employment_revision)
             =(s.org_id,s.employee_id,s.employment_revision)
        JOIN aw_bindings b ON b.token_digest=d.binding_digest AND b.installation_id=i.installation_id
             AND (b.org_id,b.employee_id,b.employment_revision)=(d.org_id,d.sender_id,d.sender_revision)
        JOIN aw_employees sender ON (sender.org_id,sender.employee_id,sender.employment_revision)
             =(b.org_id,b.employee_id,b.employment_revision)
        WHERE d.org_id=%s AND d.operation_id=%s AND i.installation_id=%s AND d.owner_id=emp.employee_id
          AND d.owner_revision=emp.employment_revision AND i.enabled AND s.enabled AND NOT b.revoked
          AND b.org_member AND b.grant_epoch=(d.payload->>'grant_epoch')::bigint AND b.expires_at>clock_timestamp() AND emp.status='ACTIVE' AND sender.status='ACTIVE'
          AND r.receipt_ref=%s AND r.task_id=%s AND r.generation=%s AND r.task_state NOT IN ('FAILED','CANCELLED')
          AND d.state IN ('BOUND','RECONCILING','SETTLED')
        """, (session.org_id, fence["operation_id"], session.installation_id, fence["receipt_ref"],
              party["task_id"], party["generation"])).fetchone()
    _check(row and row["nf_instance_id"] == fence["nf_instance_id"])
    expected = {"nf_instance_id": fence["nf_instance_id"], "scope_ref": fence["scope_ref"],
                "grant_epoch": fence["epoch"], "work_id": binding["world_ref"]["work_id"],
                "branch_id": binding["world_ref"]["branch_id"]}
    _check(all(row["payload"].get(k) == v for k, v in expected.items())
           and route(conn, session.org_id, row["sender_id"], row["employee_id"], row["scope_ref"]))
    return row


def attest(conn, session, binding):
    viewer = require_role(conn, session, {"workshop_manager", "review_lead"})
    _check(not frozen(conn, session.installation_id))
    manuscript = binding["manuscript_ref"]
    reviewer = _employee(conn, session, binding, binding["reviewer"])
    writers = [_employee(conn, session, binding, party) for party in binding["producers"]]
    row = conn.execute("""
        SELECT r.*,a.scope_ref,a.source_receipt,a.producer_ids,a.producer_groups,b.expires_at
        FROM aw_reviews r JOIN aw_review_artifacts a USING(org_id,artifact_ref,artifact_revision)
        JOIN aw_bindings b ON b.token_digest=%s
        WHERE r.org_id=%s AND r.operation_id=%s AND r.execution='BUDGETED_NF'
          AND r.reviewer_id=%s AND r.reviewer_revision=%s AND r.deadline>clock_timestamp()
          AND r.state IN ('PENDING','CHANGES_REQUESTED','APPROVED')
          AND r.artifact_ref=%s AND r.artifact_revision=%s AND r.content_sha=%s AND a.content_sha=r.content_sha
          AND a.scope_ref=%s
        """, (session.binding_digest, session.org_id, binding["reviewer"]["delegation"]["operation_id"],
              reviewer["employee_id"], reviewer["employment_revision"], manuscript["artifact_id"],
              manuscript["artifact_revision_id"], manuscript["sha256"], viewer["scope_ref"])).fetchone()
    _check(row and row["source_receipt"] == binding["producers"][0]["delegation"]["receipt_ref"]
           and all(w["employee_id"] in row["producer_ids"] and w["scope_ref"] == row["scope_ref"] for w in writers)
           and reviewer["scope_ref"] == row["scope_ref"])
    _require_review_team(conn, session, viewer, row)
    _independent(conn, session.org_id, reviewer, row)
    expires = min(row["deadline"], row["expires_at"], reviewer["expires_at"], *(w["expires_at"] for w in writers))
    # Confirm W4's registered substantive producers as well as every author in NF's manuscript lineage.
    subjects = [sorted(row["producer_groups"]), _group(conn, session.org_id, reviewer["employee_id"])]
    return {"binding_sha256": _digest(binding), "review_id": str(row["review_id"]),
            "policy_revision": row["policy_revision"], "subject_commitment": _digest(subjects),
            "expires_at": expires.isoformat()}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


TOOLS = {"aw_review_independence": ("Verify a frozen NF draft and reviewer assignment against current Workshop authority.",
                                   schema({"binding": _BINDING}), attest)}
