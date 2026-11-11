"""Persist an installation-wide outbound fence during paired database recovery."""

import argparse
import json
import os
from pathlib import Path

from psycopg.types.json import Jsonb

from .auth import Denied, connect


def frozen(conn, installation, *, exclusive=False):
    lock = "FOR UPDATE" if exclusive else "FOR SHARE"
    if not conn.execute(
        f"SELECT installation_id FROM aw_installations WHERE installation_id=%s {lock}",
        (installation,),
    ).fetchone():
        raise Denied("VIEW_DENIED")
    row = conn.execute(
        "SELECT frozen FROM aw_recovery_fences WHERE installation_id=%s", (installation,)
    ).fetchone()
    return bool(row and row["frozen"])


def freeze(conn, installation, reason="PAIRED_RESTORE"):
    frozen(conn, installation, exclusive=True)
    conn.execute(
        """INSERT INTO aw_recovery_fences(installation_id,epoch,frozen,reason) VALUES (%s,1,true,%s)
        ON CONFLICT (installation_id) DO UPDATE SET epoch=aw_recovery_fences.epoch+1,
        frozen=true,reason=excluded.reason,changed_at=clock_timestamp() WHERE NOT aw_recovery_fences.frozen""",
        (installation, reason),
    )


LEDGER_FIELDS = (
    "task_id",
    "receipt_ref",
    "generation",
    "task_state",
    "accounting_state",
    "attempts",
)


def active_epoch(conn, installation):
    row = conn.execute(
        "SELECT epoch FROM aw_recovery_fences WHERE installation_id=%s AND frozen", (installation,)
    ).fetchone()
    return row["epoch"] if row else None


def record_lookup(conn, installation, org, operation, epoch, response):
    ledger = {k: response[k] for k in LEDGER_FIELDS} if response else None
    conn.execute(
        """INSERT INTO aw_recovery_proofs(installation_id,org_id,operation_id,epoch,ledger)
        VALUES(%s,%s,%s,%s,%s) ON CONFLICT (installation_id,org_id,operation_id) DO UPDATE
        SET epoch=excluded.epoch,ledger=excluded.ledger""",
        (installation, org, operation, epoch, Jsonb(ledger) if ledger else None),
    )


def verified_ledger(conn, installation):
    if not frozen(conn, installation, exclusive=True):
        raise Denied("RECOVERY_FENCE_REQUIRED")
    rows = conn.execute(
        """SELECT d.operation_id,d.state,r.receipt_ref,
            p.epoch AS verified_epoch,p.ledger
        FROM aw_delegations d JOIN aw_recovery_fences f USING(installation_id)
        LEFT JOIN aw_remote_receipts r USING(org_id,operation_id)
        LEFT JOIN aw_recovery_proofs p ON (p.installation_id,p.org_id,p.operation_id,p.epoch)=
            (d.installation_id,d.org_id,d.operation_id,f.epoch)
        WHERE d.installation_id=%s AND d.state<>'RELEASED'""",
        (installation,),
    ).fetchall()
    for row in rows:
        if (
            row["state"] in {"SENDING", "RECONCILING", "WAITING_OPERATOR"}
            or row["verified_epoch"] is None
        ):
            raise Denied("REMOTE_HISTORY_MISMATCH")
        if bool(row["ledger"]) != bool(row["receipt_ref"]):
            raise Denied("REMOTE_HISTORY_MISMATCH")
    return {str(r["operation_id"]): r["ledger"] for r in rows if r["ledger"]}


def permit(conn, installation, expected):
    if expected != verified_ledger(conn, installation):
        raise Denied("REMOTE_HISTORY_MISMATCH")
    conn.execute(
        "UPDATE aw_recovery_fences SET frozen=false,reason='VERIFIED_RECEIPTS',changed_at=clock_timestamp() WHERE installation_id=%s",
        (installation,),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["freeze", "snapshot", "permit"])
    parser.add_argument("--installation", required=True)
    parser.add_argument("--nf-receipts", type=Path)
    args = parser.parse_args()
    if args.mode == "permit" and args.nf_receipts is None:
        parser.error("permit requires the verified complete NF operation/receipt mapping")
    with connect(os.environ["AW_DATABASE_URL"]) as conn:
        if args.mode == "freeze":
            freeze(conn, args.installation)
        elif args.mode == "snapshot":
            print(json.dumps(verified_ledger(conn, args.installation)))
        else:
            permit(conn, args.installation, json.loads(args.nf_receipts.read_text()))


if __name__ == "__main__":
    main()
