from novel_factory.infrastructure.capacity import CapacityPolicy, ResourceMonitor


def healthy(**updates):
    return dict({"available": True, "available_bytes": 2**30, "backlog": 30, "effective_cpus": 4, "cpu_pct": 25}, **updates)


def test_model_backpressure_overrides_idle_cpu_and_cooldown():
    policy = CapacityPolicy(6, "model", cooldown=5, target=4)
    assert policy.decide(healthy(rate_limits=1), 0) == (2, "provider_or_budget_pressure")
    assert policy.decide(healthy(), 1)[0] == 2
    assert policy.decide(healthy(), 5)[0] == 3
    assert policy.decide(healthy(budget_blocked=True), 6)[0] == 1
    assert policy.decide(healthy(unknown_calls=1), 12)[0] == 1


def test_cpu_affinity_missing_sample_and_tail_regression():
    policy = CapacityPolicy(6, "cpu", target=3, cooldown=1)
    assert policy.decide(healthy(effective_cpus=2), 0)[0] == 2
    assert policy.decide(healthy(available=False), 1) == (1, "sample_unavailable")
    assert policy.decide(healthy(), 2)[0] == 2
    assert policy.decide(healthy(observations=10, success_per_s=10, p95=2), 3)[0] == 3
    assert policy.decide(healthy(observations=10, success_per_s=7, p95=4), 4) == (1, "throughput_and_tail_regressed")
    assert ResourceMonitor().host()["affinity"] >= 1


def test_overlapping_slot_seconds_across_sample_and_shrink():
    from novel_factory.infrastructure.capacity import PoolOccupancy

    meter = PoolOccupancy(2, now=10)
    assert meter.sample(now=10) is None
    meter._transition(now=11, delta=1)
    meter._transition(now=12, delta=1)
    meter.set_target(1, now=13)
    first = meter.sample(now=14, reset=True)
    # [10,11) 0/2; [11,12) 1/2; [12,14) 2/2 including draining second slot.
    assert first == {"window_seconds": 4, "busy_slot_seconds": 5,
                     "available_slot_seconds": 8, "worker_utilization_pct": 62.5}
    meter._transition(now=15, delta=-1)
    meter._transition(now=16, delta=-1)
    second = meter.sample(now=18, reset=True)
    assert second["busy_slot_seconds"] == 3 and second["available_slot_seconds"] == 5
    assert second["worker_utilization_pct"] == 60
    assert meter.sample(now=20)["worker_utilization_pct"] == 0
    assert ResourceMonitor().host()["worker_utilization_pct"] is None


def test_idle_slots_shrink_after_cooldown_and_regrow_within_ceiling():
    policy = CapacityPolicy(4, "model", cooldown=3, target=4)
    idle = healthy(backlog=0, observations=0, worker_utilization_pct=0)
    assert policy.decide(idle, 0) == (4, "hold")
    assert policy.decide(dict(idle, in_flight=1), 3) == (4, "hold")
    assert policy.decide(idle, 6) == (4, "hold")
    assert policy.decide(idle, 12) == (1, "idle")
    assert policy.decide(healthy(), 13) == (1, "hold")
    assert policy.decide(healthy(), 15) == (2, "backlog_and_headroom")


def test_native_windows_monitor_reports_physical_headroom():
    import os
    import time

    import pytest

    if os.name != "nt":
        pytest.skip("Uses native GetSystemTimes and GlobalMemoryStatusEx")
    monitor = ResourceMonitor()
    sample = monitor.host()
    assert sample["available_bytes"] > 0
    # FILETIME CPU tick resolution differs across Windows machines.
    for _ in range(20):
        time.sleep(.02)
        cpu = monitor.host()["cpu_pct"]
        if cpu is not None:
            assert 0 <= cpu <= 100
            break
    else:
        pytest.fail("Windows CPU counters did not advance")
