"""Run as the trusted operator outside any role sandbox."""
import argparse
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from .auth import Denied, connect, digest
from .organization import bootstrap, claim_organization, employee, provisioning_installation


def enroll(conn, installation_id, org_id, employee_id, revision, hours):
    if revision < 1 or not 0 < hours <= 168:
        raise ValueError("Invalid binding lifetime or revision")
    provisioning_installation(conn, installation_id)
    org = conn.execute(
        "SELECT owner_installation_id FROM aw_organizations WHERE org_id=%s FOR SHARE", (org_id,)
    ).fetchone()
    if org is not None:
        if org["owner_installation_id"] != installation_id:
            raise Denied("ORGANIZATION_UNAVAILABLE")
        member = employee(conn, org_id, employee_id)
        if not member or member["status"] != "ACTIVE" or member["employment_revision"] != revision:
            raise Denied("STALE_EMPLOYMENT")
    token = secrets.token_urlsafe(40)
    conn.execute("""INSERT INTO aw_bindings
        (token_digest, installation_id, org_id, employee_id, employment_revision, grant_epoch, expires_at, org_member)
        VALUES (%s,%s,%s,%s,%s,1,%s,%s)""",
        (digest(token), installation_id, org_id, employee_id, revision,
         datetime.now(UTC) + timedelta(hours=hours), org is not None))
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    installation = sub.add_parser("install")
    installation.add_argument("installation_id")
    installation.add_argument("nf_instance_id")
    org = sub.add_parser("bootstrap")
    org.add_argument("org_id")
    org.add_argument("--preset", default="Minimal")
    org.add_argument("--installation", dest="installation_id")
    claim = sub.add_parser("claim-org")
    claim.add_argument("org_id")
    claim.add_argument("installation_id")
    issue = sub.add_parser("enroll")
    for name in ("installation_id", "org_id", "employee_id", "out"):
        issue.add_argument(name)
    issue.add_argument("--revision", type=int, required=True)
    issue.add_argument("--hours", type=int, default=8)
    revoke = sub.add_parser("revoke")
    for name in ("installation_id", "org_id", "employee_id"):
        revoke.add_argument(name)
    args = parser.parse_args()
    dsn = os.environ["AW_DATABASE_URL"]
    with connect(dsn) as conn:
        if args.action == "install":
            conn.execute("INSERT INTO aw_installations (installation_id,nf_instance_id) VALUES (%s,%s)",
                         (args.installation_id, args.nf_instance_id))
        elif args.action == "bootstrap":
            bootstrap(conn, args.org_id, args.preset, installation_id=args.installation_id)
        elif args.action == "claim-org":
            claim_organization(conn, args.org_id, args.installation_id)
        elif args.action == "enroll":
            token = enroll(conn, args.installation_id, args.org_id, args.employee_id, args.revision, args.hours)
            with os.fdopen(os.open(args.out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as dest:
                json.dump({"token": token, "database_url": dsn}, dest)
        else:
            conn.execute("""UPDATE aw_bindings SET revoked=true, grant_epoch=grant_epoch+1
                WHERE installation_id=%s AND org_id=%s AND employee_id=%s AND NOT revoked""",
                (args.installation_id, args.org_id, args.employee_id))


if __name__ == "__main__":
    main()
