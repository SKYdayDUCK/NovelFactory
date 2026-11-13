"""The lifecycle program sleeps and rebuilds resources without replaying work."""

from concurrent.futures import BrokenExecutor, Future, ThreadPoolExecutor
from threading import Event

import pytest

from novel_factory.infrastructure.agent_lifecycle import AgentLifecycleManager


def test_idle_agents_allocate_no_pool_and_completed_work_retires_on_both_modes():
    clock = [0.0]
    pools, events = [], []

    def create():
        pool = ThreadPoolExecutor(max_workers=2)
        pools.append(pool)
        return pool

    with AgentLifecycleManager(create, idle_seconds=30, clock=lambda: clock[0], observe=events.append) as manager:
        for _ in range(5000):
            assert manager.state == "DORMANT" and not manager.maintain()
        assert not pools
        assert manager.submit(lambda: 7).result(timeout=5) == 7
        # Future.result can return just before its completion callback runs.
        pools[0].shutdown(wait=True)
        clock[0] = 29
        assert not manager.maintain()
        clock[0] = 30
        assert manager.maintain() and manager.state == "DORMANT"
        assert manager.submit(lambda: 9).result(timeout=5) == 9
    assert len(pools) == manager.starts == manager.stops == 2
    assert [event["status"] for event in events] == [
        "WORKER_POOL_STARTED", "WORKER_POOL_RETIRED", "WORKER_POOL_STARTED", "WORKER_POOL_STOPPED"]


def test_running_call_is_never_retired_even_when_idle_deadline_passes():
    clock, started, finish = [0.0], Event(), Event()

    def call():
        started.set()
        assert finish.wait(5)
        return "one result"

    with AgentLifecycleManager(lambda: ThreadPoolExecutor(1), idle_seconds=1, clock=lambda: clock[0]) as manager:
        future = manager.submit(call)
        assert started.wait(5)
        clock[0] = 1000
        assert manager.state == "ACTIVE" and manager.idle_remaining() is None
        assert not manager.maintain()
        finish.set()
        assert future.result(timeout=5) == "one result"
    assert manager.starts == manager.stops == 1


def test_finished_future_and_broken_pool_are_not_resubmitted():
    calls = []

    class BrokenPool:
        def submit(self, function):
            calls.append(function)
            future = Future()
            future.set_exception(BrokenExecutor("injected process death"))
            return future

        def shutdown(self, wait=True):
            pass

    with AgentLifecycleManager(BrokenPool) as manager:
        future = manager.submit(lambda: "never run")
        with pytest.raises(BrokenExecutor):
            future.result()
        assert manager.idle_remaining() == 0
        assert manager.maintain()
        assert len(calls) == 1


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf")])
def test_invalid_idle_period_rejected(seconds):
    with pytest.raises(ValueError):
        AgentLifecycleManager(lambda: ThreadPoolExecutor(1), idle_seconds=seconds)
