"""Observe the Workshop and NF ledgers while the disposable Workshop database is offline."""

from contextlib import contextmanager

import psycopg
import pytest
from agent_workshop.auth import connect
from psycopg import sql


def workshop_snapshot(dsn, operation):
    with connect(dsn) as conn:
        return conn.execute(
            """SELECT d.state AS delegation, r.task_id::text, r.receipt_ref::text,
                      r.task_state, r.accounting_state, b.status AS budget_hold,
                      (SELECT count(*) FROM aw_cost_observations o
                       WHERE o.operation_id = d.operation_id) AS attempts_observed
               FROM aw_delegations d JOIN aw_remote_receipts r USING(org_id,operation_id)
               JOIN aw_budget_holds b USING(org_id,operation_id) WHERE d.operation_id=%s""",
            (operation,),
        ).fetchone()


def nf_snapshot(supervisor, task):
    with supervisor.kernel.db.uow(write=False) as repo:
        return {
            "state": repo.one("tasks", {"task_id": task})["state"],
            "attempts": [
                str(row["attempt_id"]) for row in repo.many("call_attempts", {"task_id": task})
            ],
        }


@contextmanager
def workshop_disconnected(admin, aw_dsn):
    # The physical drill owns this cluster. A separate control login stays on postgres.
    # No Workshop backend may continue to run on a cached database connection.
    with connect(aw_dsn) as conn:
        db = conn.info.dbname
    allow = sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS ").format(sql.Identifier(db))
    admin.execute(allow + sql.SQL("false"))
    try:
        sessions = admin.execute(
            """SELECT count(*) FROM pg_stat_activity
               WHERE datname = %s AND backend_type = 'client backend'""",
            (db,),
        ).fetchone()[0]
        assert sessions == 0
        with (
            pytest.raises(psycopg.OperationalError, match="not currently accepting connections"),
            connect(aw_dsn),
        ):
            pass
        yield
    finally:
        admin.execute(allow + sql.SQL("true"))
