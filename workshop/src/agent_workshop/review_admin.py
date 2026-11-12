"""Trusted offline intake of reviewed source receipts and subject-to-employee mappings."""

import argparse
import json
import os
from pathlib import Path

from .auth import Denied, connect
from .organization import lock_org
from .reviews_skills import MODES, register_artifact, register_skill


def subject(conn, org, employee_id, subject_group):
    saved = conn.execute(
        """INSERT INTO aw_review_identities VALUES (%s,%s,%s) ON CONFLICT(org_id,employee_id)
        DO UPDATE SET subject_group=EXCLUDED.subject_group WHERE aw_review_identities.subject_group=EXCLUDED.subject_group
        RETURNING subject_group""",
        (org, employee_id, subject_group),
    ).fetchone()
    if not saved:
        raise Denied("FROZEN_SUBJECT_MISMATCH")


def review_slot(conn, org, slot_id, artifact_ref, revision, mode):
    lock_org(conn, org, write=True)
    if conn.execute(
        "SELECT 1 FROM aw_delegations WHERE org_id=%s AND slot_id=%s", (org, slot_id)
    ).fetchone():
        raise Denied("SLOT_ALREADY_ASSIGNED")
    if mode not in MODES:
        raise Denied("INVALID_REVIEW_MODE")
    row = conn.execute(
        """INSERT INTO aw_review_slots SELECT org_id,slot_id,%s,%s,%s FROM aw_delegation_slots
        WHERE org_id=%s AND slot_id=%s AND config->>'role_id'='reviewer' AND enabled
        RETURNING slot_id""",
        (artifact_ref, revision, mode, org, slot_id),
    ).fetchone()
    if not row:
        raise Denied("REVIEW_SLOT_DENIED")
    conn.execute(
        "UPDATE aw_delegation_slots SET review_required=true WHERE org_id=%s AND slot_id=%s",
        (org, slot_id),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action, fields in {
        "subject": ("employee", "group"),
        "skill": ("skill_id", "revision", "namespace", "mode", "content_file"),
        "artifact": ("receipt_file",),
        "review-slot": ("slot_id", "artifact_ref", "revision", "mode"),
    }.items():
        cmd = sub.add_parser(action)
        for name in ("org", *fields):
            cmd.add_argument(name)
    args = parser.parse_args()
    with connect(os.environ["AW_DATABASE_URL"]) as conn:
        lock_org(conn, args.org, write=True)
        if args.action == "subject":
            subject(conn, args.org, args.employee, args.group)
        elif args.action == "skill":
            register_skill(
                conn,
                args.org,
                args.skill_id,
                args.revision,
                args.namespace,
                args.mode,
                Path(args.content_file).read_bytes(),
            )
        elif args.action == "artifact":
            register_artifact(
                conn, args.org, **json.loads(Path(args.receipt_file).read_text(encoding="utf-8"))
            )
        else:
            review_slot(conn, args.org, args.slot_id, args.artifact_ref, args.revision, args.mode)


if __name__ == "__main__":
    main()
