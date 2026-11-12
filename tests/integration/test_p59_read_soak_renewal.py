"""The long read fixture must renew the same generation and retain real expiry failures."""

from __future__ import annotations

import pytest
from sqlalchemy import func, text

from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from tests.service.conftest import make_client


@pytest.mark.postgres
def test_owner_renewal_preserves_fixed_read_when_original_fixture_expires(kernel_env):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel_env["kernel"], "r1_sword")
    renewed, original = [make_client(kernel_env, world) for _ in range(2)]
    ids = [actor["task"]["task_id"] for actor in (renewed, original)]
    service = WorldQueryService(kernel)
    tasks = kernel.tasks.tables["tasks"]
    with kernel.db.engine.begin() as conn:
        conn.execute(
            tasks.update()
            .where(tasks.c.task_id.in_(ids))
            .values(lease_expiry=func.clock_timestamp() + text("interval '1 minute'"))
        )
    expected = service.query(renewed["principal"], renewed["request"])
    service.query(original["principal"], original["request"])
    kernel.tasks.renew(
        renewed["task"]["task_id"],
        renewed["task"]["generation"],
        renewed["task"]["worker_id"],
        lease_seconds=3600,
    )
    # Advance only the two private fixture leases by an identical two minutes.
    # This exercises the actual PostgreSQL guards without a wall-clock sleep.
    with kernel.db.engine.begin() as conn:
        conn.execute(
            tasks.update()
            .where(tasks.c.task_id.in_(ids))
            .values(lease_expiry=tasks.c.lease_expiry - text("interval '2 minutes'"))
        )
    latest = service.query(renewed["principal"], renewed["request"])
    assert latest["read_cut"]["world_ref"] == expected["read_cut"]["world_ref"]
    assert latest["payload"]["state_items"] == expected["payload"]["state_items"]
    with pytest.raises(DomainError) as expired:
        service.query(original["principal"], original["request"])
    assert expired.value.code == "LEASE_EXPIRED"
    with pytest.raises(DomainError) as cannot_resurrect:
        kernel.tasks.renew(
            original["task"]["task_id"],
            original["task"]["generation"],
            original["task"]["worker_id"],
            lease_seconds=3600,
        )
    assert cannot_resurrect.value.code == "LEASE_EXPIRED"
