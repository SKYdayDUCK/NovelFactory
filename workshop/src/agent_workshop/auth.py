import hashlib
import json
from .private_files import read_private_bytes
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row


class Denied(ValueError):
    """Only stable public codes may be returned by an MCP handler."""


@dataclass(frozen=True)
class Session:
    org_id: str
    employee_id: str
    employment_revision: int
    grant_epoch: int
    installation_id: str
    org_member: bool = False
    binding_digest: str = ""


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def authenticate(conn, token, lock=True):
    query = """SELECT org_id, employee_id, employment_revision, grant_epoch, installation_id, org_member
               FROM aw_bindings JOIN aw_installations USING (installation_id)
               WHERE token_digest=%s AND NOT revoked AND enabled
                 AND expires_at > clock_timestamp()"""
    row = conn.execute(query + (" FOR SHARE" if lock else ""), (digest(token),)).fetchone()
    if row is None:
        raise Denied("VIEW_DENIED")
    return Session(**row, binding_digest=digest(token))


def private_binding(path):
    try:
        data = json.loads(read_private_bytes(path, 16384))
    except (ValueError, OSError):
        raise Denied("INVALID_BINDING") from None
    if set(data) != {"database_url", "token"} or not all(
        isinstance(v, str) and v for v in data.values()
    ):
        raise Denied("INVALID_BINDING")
    return data
