"""Disposable database pair. Tests explicitly require a PG17 test administrator."""

from contextlib import contextmanager
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module")
def dual_databases():
    with database_pair(os.environ["AW_DUAL_TEST_ADMIN_URL"]) as urls:
        yield urls


@contextmanager
def database_pair(admin_dsn):
    admin_url = make_url(admin_dsn)
    root = Path(os.environ["AW_NF_SOURCE"]).resolve()
    here = Path(__file__).resolve().parents[1]
    suffix = uuid4().hex[:16]
    names = []
    urls = {}
    with psycopg.connect(
        admin_url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    ) as conn:
        try:
            for prefix, cwd, variable in (
                ("nf", root, "NF_DATABASE_URL"),
                ("aw", here, "AW_DATABASE_URL"),
            ):
                name = f"{prefix}_test_{suffix}"
                password = secrets.token_urlsafe(24)
                ident = sql.Identifier(name)
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, sql.Literal(password))
                )
                names.append(name)
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(ident, ident))
                conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(ident))
                uri = admin_url.set(
                    username=name,
                    password=password,
                    database=name,
                    drivername="postgresql+psycopg" if prefix == "nf" else "postgresql",
                )
                urls[prefix] = uri.render_as_string(hide_password=False)
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=cwd,
                    env={**os.environ, variable: urls[prefix]},
                    check=True,
                )
            aw = make_url(urls["aw"])
            nf = make_url(urls["nf"])
            for a, b in ((aw, nf), (nf, aw)):
                with pytest.raises(psycopg.OperationalError):
                    with psycopg.connect(
                        a.set(drivername="postgresql", database=b.database).render_as_string(
                            hide_password=False
                        )
                    ):
                        pass
            yield urls
        finally:
            for name in reversed(names):
                ident = sql.Identifier(name)
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
                conn.execute(sql.SQL("DROP ROLE {}").format(ident))
