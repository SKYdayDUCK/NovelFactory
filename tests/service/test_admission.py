import asyncio

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.admission import AdmissionController, ConnectionBudget, TokenBucketLimiter

pytestmark = pytest.mark.unit


def test_per_principal_round_robin_prevents_one_principal_from_starving_another():
    async def scenario():
        controller = AdmissionController(1, per_principal=1, max_waiters=8)
        order, permits = [], {name: asyncio.Event() for name in ("a1", "a2", "b1")}

        async def use(key, name):
            async with controller.slot(key):
                order.append(name)
                await permits[name].wait()

        async with controller.slot("holder"):
            a1 = asyncio.create_task(use("a", "a1"))
            a2 = asyncio.create_task(use("a", "a2"))
            b1 = asyncio.create_task(use("b", "b1"))
            await asyncio.sleep(0)
            assert controller.snapshot()["queued"] == 3
        await asyncio.sleep(0)
        permits["a1"].set()
        await a1
        permits["b1"].set()
        await b1
        permits["a2"].set()
        await a2
        assert order == ["a1", "b1", "a2"]
        assert controller.snapshot()["peak_active"] == 1
        assert controller.snapshot()["active"] == controller.snapshot()["queued"] == 0

    asyncio.run(scenario())


def test_saturated_principal_does_not_block_an_available_principal():
    async def scenario():
        controller = AdmissionController(2, per_principal=1)
        finished = asyncio.Event()

        async def same_principal():
            async with controller.slot("a"):
                finished.set()

        async with controller.slot("a"):
            waiting = asyncio.create_task(same_principal())
            await asyncio.sleep(0)
            async with controller.slot("b"):
                assert not finished.is_set()
                assert controller.snapshot()["active"] == 2
        await waiting
        assert finished.is_set()

    asyncio.run(scenario())


def test_queue_overflow_is_explicit_and_cancelled_waiter_releases_its_place():
    async def scenario():
        controller = AdmissionController(1, max_waiters=1)

        async def wait():
            async with controller.slot("waiting"):
                raise AssertionError("cancelled waiter must not enter")

        async with controller.slot("busy"):
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            with pytest.raises(DomainError) as failure:
                async with controller.slot("overflow"):
                    pass
            assert failure.value.code == "BACKPRESSURE"
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert controller.snapshot()["queued"] == 0
        async with controller.slot("new"):
            assert controller.snapshot()["active"] == 1
        assert controller.snapshot()["rejected"] == 1

    asyncio.run(scenario())


def test_wait_deadline_does_not_leak_an_active_slot():
    async def scenario():
        controller = AdmissionController(1, timeout=0.01)
        async with controller.slot("busy"):
            with pytest.raises(DomainError) as failure:
                async with controller.slot("waiting"):
                    pass
            assert failure.value.code == "BACKPRESSURE"
            assert controller.snapshot()["active"] == 1
            assert controller.snapshot()["queued"] == 0
        assert controller.snapshot()["active"] == 0
        assert controller.snapshot()["timed_out"] == 1

    asyncio.run(scenario())


def test_category_budget_preserves_capacity_for_queries():
    async def scenario():
        controller = AdmissionController(2, class_limits={"write": 1, "query": 2})
        async with controller.slot("a", "write"):
            async with controller.slot("b", "query"):
                assert controller.snapshot()["active"] == 2
    asyncio.run(scenario())


def test_rate_limit_and_refill_use_authenticated_principal():
    async def scenario():
        clock = [100.0]
        limiter = TokenBucketLimiter(rate=2, burst=2, clock=lambda: clock[0])
        await limiter.check("a")
        await limiter.check("a")
        with pytest.raises(DomainError) as failure:
            await limiter.check("a")
        assert failure.value.code == "RATE_LIMITED"
        await limiter.check("b")
        clock[0] += 0.5
        await limiter.check("a")
    asyncio.run(scenario())


def test_deployment_connection_budget_counts_all_processes():
    budget = ConnectionBudget(api_processes=2, api_pool_size=8, worker_processes=2, worker_pool_size=4)
    budget.validate()
    assert budget.total == 40
    with pytest.raises(ValueError, match="aggregate"):
        ConnectionBudget(api_processes=8).validate()

def test_connection_inventory_counts_child_pools_stdio_and_listeners():
    inventory = dict(active_stdio_processes=3, compute_child_processes=4, listen_connections=2)
    budget = ConnectionBudget(**inventory, postgres_max_connections=40)
    budget.validate()
    assert budget.total == 40
    with pytest.raises(ValueError, match="aggregate"):
        ConnectionBudget(**inventory, postgres_max_connections=39).validate()



def test_status_and_control_can_bypass_saturated_class_within_same_principal():
    async def scenario():
        controller = AdmissionController(3, per_principal=3, fast_slots=1)
        order = []
        async def record(name, kind):
            async with controller.slot("same", kind):
                order.append(name)
        async with controller.slot("same", "query"), controller.slot("same", "write"):
            queued = asyncio.create_task(record("third-heavy", "context"))
            await asyncio.sleep(0)
            await asyncio.wait_for(record("status", "fast"), timeout=1)
            assert order == ["status"] and controller.snapshot()["queued"] == 1
        await queued
        assert order == ["status", "third-heavy"]
    asyncio.run(scenario())
