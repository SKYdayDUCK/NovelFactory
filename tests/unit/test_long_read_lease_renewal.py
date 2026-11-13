"""A long read probe renews the exact live lease for each reader."""
import asyncio
from types import SimpleNamespace

from benchmarks import p3_stability


def test_hour_probe_renews_bound_workers_and_stops(monkeypatch):
    renewed = []
    def renew(task_id, generation, owner, *, lease_seconds):
        renewed.append((task_id, generation, owner, lease_seconds))

    async def read_until_renewed(*args, **kwargs):
        async with asyncio.timeout(3):
            while len(renewed) < 2:
                await asyncio.sleep(.005)
        return {"scheduled_count": 1}

    monkeypatch.setattr(p3_stability, "steady", read_until_renewed)
    kernel = SimpleNamespace(tasks=SimpleNamespace(renew=renew))
    fixture = {"population": [{"task_id": "first", "generation": 1}, {"task_id": "second", "generation": 4}]}
    result = asyncio.run(p3_stability.steady_with_renewal(None, kernel, fixture, seconds=1,
                                                         rate=1, renew_every=.02))
    assert renewed[:2] == [("first", 1, "load:first", 3600), ("second", 4, "load:second", 3600)]
    assert sum(c["leases"] for c in result["worker_lease_renewals"]) == len(renewed)
    assert result["scheduled_count"] == 1
