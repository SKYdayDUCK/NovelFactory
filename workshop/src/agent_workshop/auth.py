import hashlib
import json
import os
import stat
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
    binding_digest: str = ''


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
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as handle:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077 or meta.st_size > 16384):
            raise Denied("INVALID_BINDING")
        data = json.load(handle)
    if set(data) != {"database_url", "token"} or not all(
        isinstance(v, str) and v for v in data.values()
    ):
        raise Denied("INVALID_BINDING")
    return data
