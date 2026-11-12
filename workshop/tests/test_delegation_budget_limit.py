"""Operator budget changes and employee reservations serialize on one PostgreSQL account."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.delegation import delegate
from test_delegation import make, setup_slot


def test_operator_cannot_cut_below_settlements_and_an_inflight_hold():
    dsn, org, _, token = setup_slot()
    app = "w3-limit-" + uuid4().hex
    executable = Path(sys.executable).with_name("aw-provision")

    def set_limit(limit, currency="USD"):
        return subprocess.run(
            [executable, "budget", org, currency, limit],
            env={**os.environ, "AW_DATABASE_URL": dsn, "PGAPPNAME": app},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    # Seed a historical settlement at its recorded cost, below the original hold.
    spent = make(dsn, token, slot="scene-A")
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE aw_budget_holds SET status='SETTLED',settled_amount=0.25 WHERE org_id=%s AND operation_id=%s",
            (org, spent),
        )

    assert set_limit("0.25").returncode == 0
    with pytest.raises(Denied, match="BUDGET_EXCEEDED"):
        make(dsn, token, key="next", slot="scene-B")
    assert set_limit("2").returncode == 0

    # Keep a legitimate new reservation open. Observe the operator actually
    # waiting on that transaction before allowing the reservation to commit.
    with ThreadPoolExecutor(max_workers=1) as pool, connect(dsn) as reservation:
        delegate(reservation, authenticate(reservation, token), "next", "scene-B", "1", 200)
        lowering = pool.submit(set_limit, "1")
        with connect(dsn) as observer:
            observer.autocommit = True
            deadline = monotonic() + 12
            while not observer.execute(
                """SELECT 1 FROM pg_stat_activity
                   WHERE application_name=%s AND %s=ANY(pg_blocking_pids(pid))""",
                (app, reservation.info.backend_pid),
            ).fetchone():
                assert monotonic() < deadline, "operator did not block behind the reservation"
                sleep(0.025)
        reservation.commit()
        refused = lowering.result(timeout=12)
    assert refused.returncode != 0 and "BUDGET_BELOW_COMMITTED" in refused.stderr

    def account():
        with connect(dsn) as conn:
            return conn.execute(
                "SELECT currency,limit_amount FROM aw_budget_accounts WHERE org_id=%s",
                (org,),
            ).fetchone()

    assert account() == {"currency": "USD", "limit_amount": Decimal(2)}
    assert set_limit("1.25").returncode == 0
    too_low = set_limit("1.249999")
    assert too_low.returncode != 0 and "BUDGET_BELOW_COMMITTED" in too_low.stderr
    wrong_currency = set_limit("2", "CNY")
    assert wrong_currency.returncode != 0 and "CURRENCY_MISMATCH" in wrong_currency.stderr
    assert account() == {"currency": "USD", "limit_amount": Decimal("1.25")}
