"""Run as the trusted operator outside any role sandbox."""
import argparse
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from .auth import connect, digest


def enroll(conn, installation_id, org_id, employee_id, revision, hours):
    if revision < 1 or not 0 < hours <= 168:
        raise ValueError("Invalid binding lifetime or revision")
    token = secrets.token_urlsafe(40)
    conn.execute("""INSERT INTO aw_bindings
        (token_digest, installation_id, org_id, employee_id, employment_revision, grant_epoch, expires_at)
        VALUES (%s,%s,%s,%s,%s,1,%s)""",
        (digest(token), installation_id, org_id, employee_id, revision,
         datetime.now(UTC) + timedelta(hours=hours)))
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    installation = sub.add_parser("install")
    installation.add_argument("installation_id")
    installation.add_argument("nf_instance_id")
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
