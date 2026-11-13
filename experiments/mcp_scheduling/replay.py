"""Finite, single-host dispatch and routing replay. All durations are simulated ticks."""

import argparse
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
import gzip
import hashlib
import heapq
import json
import math
from pathlib import Path
import platform
import random
import sys


REPLICAS = 4
QUEUE_LIMIT = 24
LEASE_DEADLINE = 20
ELIGIBLE = {"alpha": (0, 1, 2, 3), "beta": (1, 2, 3), "gamma": (2, 3), "delta": (0,)}
KINDS = ("read", "context", "lease", "model")
COST = {"read": 1, "context": 3, "lease": 1, "model": 2}


@dataclass(frozen=True)
class Arm:
    queue: str = "fifo"
    router: str = "rr"
    ageing: bool = False
    reserve: bool = False
    adaptive: bool = False
    detach: bool = False


ARMS = {
    "baseline": Arm(),
    "wfq": Arm(queue="wfq"),
    "drr": Arm(queue="drr"),
    "drr-age": Arm(queue="drr", ageing=True),
    "reserved": Arm(reserve=True),
    "adaptive": Arm(adaptive=True),
    "least": Arm(router="least"),
    "p2": Arm(router="p2"),
    "detached": Arm(detach=True),
    "combined": Arm("drr", "p2", True, True, True, True),
}


@dataclass(frozen=True)
class Request:
    seq: int
    due: int
    work: str
    kind: str
    service: int
    remote_wait: int


def workload(profile, seed):
    randomizer = random.Random(seed)
    result = []
    for seq in range(192):
        due = {"low": seq * 18, "sustained": seq, "burst": (seq // 48) * 24}[profile]
        work = randomizer.choices(tuple(ELIGIBLE), (70, 15, 10, 5))[0]
        kind = randomizer.choices(KINDS, (61, 22, 10, 7))[0]
        service = {"read": 2, "context": 7, "lease": 2, "model": 2}[kind]
        lag = 42 + randomizer.randrange(15) if kind == "model" else 0
        result.append(Request(seq, due, work, kind, service, lag))
    return result


@dataclass
class Item:
    request: Request
    enqueued: int
    order: int
    resume: bool = False
    finish_tag: float = 0.0

    @property
    def flow(self):
        return self.request.work, self.request.kind == "lease"


class Waiting:
    """Each replica owns a bounded transient queue. WFQ tags use the fluid GPS reference."""

    def __init__(self, arm):
        self.arm = arm
        self.buckets = {}
        self.rotation = deque()
        self.credit = {}
        self.visiting = False
        self.count = 0
        self.virtual = 0.0
        self.last_tick = 0
        self.last_tag = {}

    @staticmethod
    def weight(flow):
        return 2 if flow[1] else 1

    def gps_clock(self, tick):
        elapsed = tick - self.last_tick
        self.last_tick = tick
        while elapsed > 0:
            active = {f: tag for f, tag in self.last_tag.items() if tag > self.virtual}
            if not active:
                break
            weight = sum(self.weight(f) for f in active)
            next_tag = min(active.values())
            interval = (next_tag - self.virtual) * weight
            if elapsed < interval:
                self.virtual += elapsed / weight
                break
            self.virtual = next_tag
            elapsed -= interval
        self.last_tag = {f: tag for f, tag in self.last_tag.items() if tag > self.virtual}

    def push(self, item):
        if self.count >= QUEUE_LIMIT:
            return False
        if self.arm.reserve and not item.flow[1]:
            own = sum(len(q) for f, q in self.buckets.items() if f[0] == item.flow[0])
            if self.count >= QUEUE_LIMIT - 2 or own >= QUEUE_LIMIT // 2:
                return False
        flow = item.flow
        if flow not in self.buckets:
            self.buckets[flow] = deque()
            self.rotation.append(flow)
            self.credit[flow] = 0
        if self.arm.queue == "wfq":
            self.gps_clock(item.enqueued)
            start = max(self.virtual, self.last_tag.get(flow, self.virtual))
            item.finish_tag = start + COST[item.request.kind] / self.weight(flow)
            self.last_tag[flow] = item.finish_tag
        self.buckets[flow].append(item)
        self.count += 1
        return True

    def drr_head(self):
        while True:
            flow = self.rotation[0]
            if not self.visiting:
                self.credit[flow] += 2 * self.weight(flow)
                self.visiting = True
            cost = COST[self.buckets[flow][0].request.kind]
            if self.credit[flow] >= cost:
                return flow
            self.rotation.rotate(-1)
            self.visiting = False

    def pop(self, tick):
        oldest = min(self.buckets, key=lambda f: self.buckets[f][0].order)
        if self.arm.ageing and tick - self.buckets[oldest][0].enqueued >= 18:
            flow = oldest
        elif self.arm.queue == "fifo":
            flow = oldest
        elif self.arm.queue == "wfq":
            flow = min(self.buckets, key=lambda f: (self.buckets[f][0].finish_tag,
                                                      self.buckets[f][0].order))
        else:
            flow = self.drr_head()
        item = self.buckets[flow].popleft()
        self.count -= 1
        self.credit[flow] = max(0, self.credit[flow] - COST[item.request.kind])
        if not self.buckets[flow]:
            if self.rotation[0] == flow:
                self.visiting = False
            self.rotation.remove(flow)
            del self.credit[flow], self.buckets[flow]
        return item


class Router:
    def __init__(self, strategy, seed):
        self.strategy = strategy
        self.position = Counter()
        self.randomizer = random.Random(seed)

    def select(self, work, outstanding):
        eligible = ELIGIBLE[work]
        if self.strategy == "rr":
            index = self.position[work] % len(eligible)
            self.position[work] += 1
            return eligible[index]
        candidates = self.randomizer.sample(eligible, len(eligible) if self.strategy == "least"
                                             else min(2, len(eligible)))
        return min(candidates, key=outstanding.__getitem__)


@dataclass
class Trace:
    seq: int
    work: str
    kind: str
    due: int
    replica: int = -1
    admitted: int | None = None
    first: int | None = None
    wake: int | None = None
    end: int | None = None
    state: str = "offered"
    demand: int = 0
    wait_demand: int = 0
    occupied: list = field(default_factory=list)


def percentile(values, p):
    return sorted(values)[math.ceil(len(values) * p) - 1] if values else None


def replay(requests, arm, seed):
    queues = [Waiting(arm) for _ in range(REPLICAS)]
    router = Router(arm.router, seed)
    rows = {r.seq: Trace(r.seq, r.work, r.kind, r.due, demand=r.service, wait_demand=r.remote_wait)
            for r in requests}
    # At equal tick, work finishes first, then wakes and new arrivals compete for slots.
    events = [(r.due, 2, r.seq, "arrive", r) for r in requests]
    heapq.heapify(events)
    busy = [False] * REPLICAS
    external = set()
    peaks = Counter()
    limit = 1 if arm.adaptive else REPLICAS
    next_tune = 0
    order = 0
    cursor = 0
    end = 0
    limit_changes = [(0, limit)]
    while events or any(q.count for q in queues):
        tick = events[0][0] if events else end
        event_tick = tick
        if arm.adaptive:
            tick = min(tick, next_tune)
        end = tick
        while events and events[0][0] == tick:
            _, _, _, stage, req = heapq.heappop(events)
            row = rows[req.seq]
            if stage == "complete":
                busy[row.replica] = False
                row.end, row.state = tick, "done"
                continue
            if stage == "release":
                busy[row.replica] = False
                external.add(req.seq)
                heapq.heappush(events, (tick + req.remote_wait, 1, req.seq, "wake", req))
                continue
            if stage == "wake":
                external.remove(req.seq)
                row.wake = tick
            else:
                load = [q.count + int(b) for q, b in zip(queues, busy, strict=True)]
                row.replica = router.select(req.work, load)
            item = Item(req, tick, order, resume=stage == "wake")
            order += 1
            if not queues[row.replica].push(item):
                row.end = tick
                row.state = "wake_rejected" if item.resume else "rejected"
            elif not item.resume:
                row.admitted = tick
        backlog = sum(q.count for q in queues)
        peaks["queued"] = max(peaks["queued"], backlog)
        peaks["replica_queue"] = max(peaks["replica_queue"], max(q.count for q in queues))
        if arm.adaptive and tick == next_tune:
            # Permit one more slot per control interval, up to the identical fixed cap.
            previous = limit
            if backlog >= 2:
                limit = min(REPLICAS, limit + 1)
            elif backlog == 0 and sum(busy) <= limit - 1:
                limit = max(1, limit - 1)
            if limit != previous:
                limit_changes.append((tick, limit))
            next_tune = tick + 4
            if not backlog and not any(busy) and tick < event_tick:
                next_tune = max(next_tune, event_tick)
        # Rotate the first checked replica so global caps have no fixed-index priority.
        for offset in range(REPLICAS):
            worker = (cursor + offset) % REPLICAS
            if busy[worker] or not queues[worker].count or sum(busy) >= limit:
                continue
            item = queues[worker].pop(tick)
            req = item.request
            row = rows[req.seq]
            assert row.replica == worker and worker in ELIGIBLE[req.work]
            if row.first is None:
                row.first = tick
            busy[worker] = True
            slow = 2 if worker == 0 else 1
            service = req.service * slow
            stage = "complete"
            if req.remote_wait and arm.detach:
                service = slow
                stage = "complete" if item.resume else "release"
            elif req.remote_wait:
                service += req.remote_wait
            row.occupied.append((tick, tick + service))
            heapq.heappush(events, (tick + service, 0, req.seq, stage, req))
        cursor = (cursor + 1) % REPLICAS
        peaks["busy"] = max(peaks["busy"], sum(busy))
        peaks["external_wait"] = max(peaks["external_wait"], len(external))
    assert not external and not any(busy)
    result = list(rows.values())
    done = [r for r in result if r.state == "done"]
    leases = [r for r in result if r.kind == "lease"]
    timely = [r for r in leases if r.state == "done" and r.end - r.due <= LEASE_DEADLINE]
    effective = len(done) - sum(r.kind == "lease" for r in done) + len(timely)
    duration = max((r.end for r in result), default=0) - min((r.due for r in result), default=0)
    latencies = [r.end - r.due for r in done]
    per_work = {w: dict(Counter(r.state for r in result if r.work == w)) for w in ELIGIBLE}
    per_kind = {k: dict(Counter(r.state for r in result if r.kind == k)) for k in KINDS}
    summary = {"offered": len(result), "states": dict(Counter(r.state for r in result)),
               "per_work": per_work, "per_kind": per_kind,
               "leases": len(leases), "timely_leases": len(timely), "effective": effective,
               "elapsed": duration, "effective_per_tick": effective / duration if duration else None,
               "p50": percentile(latencies, .50), "p95": percentile(latencies, .95),
               "p99": percentile(latencies, .99), "peaks": dict(peaks),
               "slot_ticks": sum(b - a for r in result for a, b in r.occupied),
               "drain_ticks": max((r.end for r in result), default=0)
               - max((r.due for r in result), default=0), "slot_limit_changes": limit_changes}
    return summary, result


def run(directory):
    directory.mkdir(parents=True, exist_ok=True)
    summaries = []
    raw_path = directory / "requests.jsonl.gz"
    with raw_path.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as raw:
        for profile in ("low", "sustained", "burst"):
            for seed in (17, 29, 53):
                requests = workload(profile, seed)
                for candidate in list(ARMS)[1:]:
                    previous = {}
                    for index, name in enumerate(("baseline", candidate, candidate, "baseline")):
                        summary, rows = replay(requests, ARMS[name], seed)
                        digest = hashlib.sha256()
                        key = [profile, seed, candidate, index, name]
                        for row in rows:
                            event = asdict(row)
                            record = json.dumps(event, sort_keys=True, separators=(",", ":"))
                            digest.update(record.encode())
                            line = json.dumps([*key, event], sort_keys=True, separators=(",", ":"))
                            raw.write((line + "\n").encode())
                        value = digest.hexdigest()
                        assert previous.get(name, value) == value
                        previous[name] = value
                        summaries.append({"profile": profile, "seed": seed, "candidate": candidate,
                                          "order": index, "arm": name, "trace_sha256": value,
                                          **summary})
    metadata = {"evidence": "synthetic discrete-event replay", "python": sys.version,
                "platform": platform.platform(), "input_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()).hexdigest(), "arm_config": {k: asdict(v)
                    for k, v in ARMS.items()}, "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "rows": summaries}
    (directory / "summary.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
