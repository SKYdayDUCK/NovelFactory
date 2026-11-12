"""Disposable service databases with independent PostgreSQL login identities."""

import os
import secrets
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url


@pytest.fixture(scope="module")
def dual_databases():
    with database_pair(
        os.environ["AW_DUAL_TEST_ADMIN_URL"], os.environ.get("AW_DUAL_TEST_NF_ADMIN_URL")
    ) as urls:
        yield urls


@contextmanager
def database_pair(admin_dsn, nf_admin_dsn=None):
    aw_admin = make_url(admin_dsn)
    nf_admin = make_url(nf_admin_dsn) if nf_admin_dsn else aw_admin
    root = Path(os.environ["AW_NF_SOURCE"]).resolve()
    workshop = Path(__file__).resolve().parents[1]
    suffix = uuid4().hex[:16]
    urls = {}
    with ExitStack() as stack:
        systems = {}
        for prefix, admin, cwd, variable in (
            ("nf", nf_admin, root, "NF_DATABASE_URL"),
            ("aw", aw_admin, workshop, "AW_DATABASE_URL"),
        ):
            conn = stack.enter_context(
                psycopg.connect(
                    admin.set(drivername="postgresql").render_as_string(hide_password=False),
                    autocommit=True,
                )
            )
            systems[prefix] = conn.execute(
                "SELECT system_identifier FROM pg_control_system()"
            ).fetchone()[0]
            name = f"{prefix}_test_{suffix}"
            password = secrets.token_urlsafe(24)
            ident = sql.Identifier(name)
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, sql.Literal(password))
            )
            stack.callback(conn.execute, sql.SQL("DROP ROLE {}").format(ident))
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(ident, ident))
            stack.callback(conn.execute, sql.SQL("DROP DATABASE {} WITH (FORCE)").format(ident))
            conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(ident))
            uri = admin.set(
                username=name,
                password=password,
                database=name,
                drivername="postgresql+psycopg" if prefix == "nf" else "postgresql",
            )
            urls[prefix] = uri.render_as_string(hide_password=False)
            with psycopg.connect(
                uri.set(drivername="postgresql").render_as_string(hide_password=False)
            ) as service:
                assert service.execute(
                    "SELECT current_user, current_database(), rolsuper, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = current_user"
                ).fetchone() == (name, name, False, False, False)
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=cwd,
                env={**os.environ, variable: urls[prefix]},
                check=True,
            )
        if nf_admin_dsn:
            assert systems["nf"] != systems["aw"], "separate service clusters are required"
        aw, nf = (make_url(urls[key]) for key in ("aw", "nf"))
        for caller, target in ((aw, nf), (nf, aw)):
            cross = target.set(
                drivername="postgresql", username=caller.username, password=caller.password
            )
            with (
                pytest.raises(psycopg.OperationalError),
                psycopg.connect(cross.render_as_string(hide_password=False)),
            ):
                pass
        yield urls
