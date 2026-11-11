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
