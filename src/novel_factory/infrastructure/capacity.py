"""Bounded process-local concurrency control. PG admission, grants and budgets stay authoritative."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout


def _read(path):
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _kv(path):
    return {line.split()[0].rstrip(":"): int(line.split()[1]) for line in _read(path).splitlines()
            if len(line.split()) >= 2 and line.split()[1].isdigit()}


def _number(path):
    raw = _read(path)
    return int(raw) if raw.isdigit() else None


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else None


class ResourceMonitor:
    def __init__(self):
        relative = next((row[3:].lstrip("/") for row in _read("/proc/self/cgroup").splitlines()
                         if row.startswith("0::")), "")
        mount = Path("/sys/fs/cgroup")
        self.cg = mount / relative if (mount / relative / "cpu.stat").exists() else mount
        self.previous = None
        self.results = deque(maxlen=4096)
        self.sample_time = time.monotonic()
        self.pool_timeouts = 0

    def completed(self, elapsed, ok=True):
        self.results.append((time.monotonic(), elapsed, ok))

    def host(self):
        now = time.monotonic()
        lines = _read("/proc/stat").splitlines()
        ticks = [int(v) for v in lines[0].split()[1:9]] if lines and lines[0].startswith("cpu ") else []
        total, idle = (sum(ticks), sum(ticks[3:5])) if ticks else (0, 0)
        process = time.process_time()
        cpu_stat = _kv(self.cg / "cpu.stat")
        use, throttle = cpu_stat.get("usage_usec"), cpu_stat.get("throttled_usec")
        affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
        quota = _read(self.cg / "cpu.max").split()
        quota_cpus = int(quota[0]) / int(quota[1]) if len(quota) == 2 and quota[0].isdigit() else None
        usable = min(affinity, quota_cpus) if quota_cpus else affinity
        cpu_pct = process_pct = throttle_pct = None
        if self.previous:
            before, old_total, old_idle, old_process, old_use, old_throttle = self.previous
            delta = max(.000001, now - before)
            process_pct = 100 * (process - old_process) / delta
            if old_use is not None and use is not None:
                cpu_pct = min(100, 100 * (use - old_use) / (delta * 1e6 * usable))
            elif total > old_total:
                cpu_pct = 100 * (1 - (idle - old_idle) / (total - old_total))
            if old_throttle is not None and throttle is not None:
                throttle_pct = 100 * (throttle - old_throttle) / (delta * 1e6)
        self.previous = (now, total, idle, process, use, throttle)
        psi_file = _read(self.cg / "cpu.pressure") or _read("/proc/pressure/cpu")
        psi = next((float(v[6:]) for line in psi_file.splitlines() if line.startswith("some ")
                    for v in line.split() if v.startswith("avg10=")), None)
        mem = _kv("/proc/meminfo")
        mem_limit, current = _number(self.cg / "memory.max"), _number(self.cg / "memory.current")
        available = mem.get("MemAvailable")
        if available is not None:
            available *= 1024
        if mem_limit is not None and current is not None:
            available = min(available if available is not None else mem_limit, max(0, mem_limit - current))
        children = _read(f"/proc/self/task/{os.getpid()}/children").split()
        child_rss = [_kv(f"/proc/{pid}/status").get("VmRSS") for pid in children if pid.isdigit()]
        samples = [(elapsed, ok) for ended, elapsed, ok in self.results if ended >= self.sample_time]
        interval = max(.001, now - self.sample_time)
        self.sample_time = now
        costs = [elapsed for elapsed, ok in samples if ok]
        return {"cpu_pct": cpu_pct, "cpu_count": os.cpu_count(), "affinity": affinity, "quota_cpus": quota_cpus,
                "effective_cpus": usable, "process_cpu_pct": process_pct, "throttle_pct": throttle_pct, "cpu_psi": psi,
                "available_bytes": available, "rss_kib": _kv("/proc/self/status").get("VmRSS"),
                "child_rss_kib": sum(size for size in child_rss if size is not None),
                "swap_used_kib": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                "oom_kill": _kv(self.cg / "memory.events").get("oom_kill"),
                "success_per_s": len(costs) / interval, "observations": len(samples),
                "failure_rate": 1 - len(costs) / len(samples) if samples else 0,
                "p95": percentile(costs, .95), "p99": percentile(costs, .99), "pool_timeouts": self.pool_timeouts}

    def sample(self, db, group, work=None, branch=None, redis=None):
        stats = self.host()
        try:
            start = time.monotonic()
            with db.uow(write=False) as repo:
                stats["pool_wait_ms"] = (time.monotonic() - start) * 1000
                stats.update(repo.conn.execute(text("""
                    SELECT count(*) FILTER(WHERE state='active') AS active_connections,
                      count(*) FILTER(WHERE wait_event_type='Lock') AS lock_waiters,
                      COALESCE(max(EXTRACT(epoch FROM now()-xact_start)) FILTER(WHERE state='active'),0)::float8 AS max_tx_s
                    FROM pg_stat_activity WHERE datname=current_database()
                """)).mappings().one())
                scope = {"work": work, "branch": branch, "group": group}
                if work is not None:
                    stats.update(repo.conn.execute(text("""
                        SELECT count(*) FILTER(WHERE state='READY') AS ready,
                          COALESCE(max(EXTRACT(epoch FROM now()-updated_at)) FILTER(WHERE state='READY'),0)::float8 AS oldest_ready_s
                        FROM nf_tasks WHERE branch_id=:branch AND work_id=:work
                    """), scope).mappings().one())
                if work is None:
                    stats["queue_backlog"] = repo.conn.scalar(text("""SELECT count(*) FROM nf_bus_subscriptions
                        WHERE consumer=:group AND status='PENDING'"""), scope)
                elif group == "model":
                    stats["enrolled_ready"] = repo.conn.scalar(text("""SELECT count(*) FROM (
                        SELECT t.task_id FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                        WHERE t.branch_id=:branch AND t.work_id=:work AND t.state='READY'
                          AND t.deadline > statement_timestamp()
                          AND NOT EXISTS (SELECT FROM nf_call_attempts c WHERE c.task_id=t.task_id) LIMIT 1024
                    ) eligible"""), scope)
                elif group in {"world", "recovery"}:
                    stats["project_hint"] = repo.conn.scalar(text("""SELECT count(*) FROM nf_processor_hints h
                        JOIN nf_redis_branches b USING(branch_id) WHERE h.branch_id=:branch AND b.work_id=:work
                        AND h.processor=:group"""), scope)
                if group == "compute" and work is not None:
                    stats["index_backlog"] = repo.conn.scalar(text("""
                        SELECT count(*) FROM (SELECT 1 FROM nf_outbox o WHERE o.topic='world.accepted'
                          AND o.payload->'world_ref'->>'branch_id'=:branch AND NOT EXISTS
                            (SELECT FROM nf_inbox i WHERE i.message_id=o.outbox_id AND i.consumer=:consumer)
                          LIMIT 1024) pending
                    """), {**scope, "consumer": "derived-world:nf-derived-world-1"})
                if group == "model" and work is not None:
                    stats.update(repo.conn.execute(text("""
                        SELECT count(*) FILTER(WHERE c.state='SENT') AS in_flight,
                          count(*) FILTER(WHERE c.state='UNKNOWN') AS unknown_calls,
                          count(*) FILTER(WHERE c.created_at>now()-interval '60 seconds') AS requests_minute,
                          COALESCE(sum(c.reserved_tokens) FILTER(WHERE c.state IN ('PREPARED','SENT','UNKNOWN')),0)::bigint AS reserved_tokens
                        FROM nf_call_attempts c JOIN nf_tasks t USING(task_id) WHERE t.work_id=:work
                    """), scope).mappings().one())
                    stats.update(repo.conn.execute(text("""
                        SELECT count(*) FILTER(WHERE payload->>'reason'='MODEL_RATE_LIMITED') AS rate_limits,
                          max((payload->>'elapsed_seconds')::float8) AS provider_latency_s
                        FROM nf_artifacts WHERE work_id=:work AND kind='AGENT_CALL_EVIDENCE'
                          AND created_at>now()-interval '60 seconds'
                    """), scope).mappings().one())
                    stats["budget_blocked"] = repo.conn.scalar(text("""
                        SELECT EXISTS (SELECT FROM nf_supervisor_jobs j JOIN nf_tasks t USING(task_id)
                          JOIN nf_budgets b ON b.budget_id::text=j.config->'dispatch'->>'budget_id'
                          JOIN nf_task_inputs i ON i.task_id=t.task_id AND i.generation=t.generation
                          WHERE t.branch_id=:branch AND t.state='READY' AND
                           (b.limit_amount-b.spent_amount-b.reserved_amount < (j.config->'dispatch'->>'reserve_amount')::numeric
                            OR (i.spec->'budget'->>'token_budget')::int < (j.config->'dispatch'->>'token_reservation')::int))
                    """), scope)
            stats["sample_sql_ms"] = (time.monotonic() - start) * 1000 - stats["pool_wait_ms"]
            if redis is not None:
                from novel_factory.infrastructure.event_streams import STREAMS, SUBSCRIBERS
                pending, lag = 0, 0
                for key, stream in STREAMS.items():
                    if group not in SUBSCRIBERS[key] or not redis.exists(stream):
                        continue
                    for row in redis.xinfo_groups(stream):
                        name = row["name"]
                        if (name.decode() if isinstance(name, bytes) else name) == group:
                            pending += row["pending"]
                            lag += row.get("lag") or 0
                stats.update(redis_pending=pending, redis_lag=lag)
            if work is None:
                stats["backlog"] = max(stats.get("queue_backlog", 0), stats.get("redis_lag", 0))
            else:
                # Stream groups span every work. Only dispatchable local work can grow this project executor.
                stats["backlog"] = max(stats.get(k, 0) for k in ("enrolled_ready", "index_backlog", "project_hint"))
            stats["available"] = stats.get("available_bytes") is not None
        except PoolTimeout:
            self.pool_timeouts += 1
            stats.update(available=False, pool_timeouts=self.pool_timeouts)
        except (RedisError, SQLAlchemyError, OSError, ValueError, KeyError):
            # Errors can contain DSNs; emit no raw diagnostics from telemetry.
            stats.update(available=False)
        return stats


@dataclass
class CapacityPolicy:
    maximum: int
    resource: str
    cooldown: float = 3
    minimum: int = 1
    target: int = 1
    cpu_ema: float | None = None
    next_growth: float = 0
    previous_rate: float = 0
    previous_tail: float = 0

    def __post_init__(self):
        if not 1 <= self.minimum <= self.target <= self.maximum <= 16 or self.cooldown <= 0:
            raise ValueError("Invalid concurrency bounds")

    def decide(self, sample, now):
        cpu = sample.get("cpu_pct")
        if cpu is not None:
            self.cpu_ema = cpu if self.cpu_ema is None else .65 * self.cpu_ema + .35 * cpu
        memory = sample.get("available_bytes")
        reason = None
        if not sample.get("available") or memory is None:
            reason = "sample_unavailable"
        elif memory < 192 * 1024 * 1024:
            reason = "memory_pressure"
        elif sample.get("lock_waiters", 0) >= 2 or sample.get("pool_wait_ms", 0) > 100:
            reason = "database_pressure"
        elif self.resource == "model" and any(sample.get(k) for k in ("rate_limits", "unknown_calls", "budget_blocked")):
            reason = "provider_or_budget_pressure"
        elif self.resource in ("cpu", "observability") and (
            (self.cpu_ema or 0) > 85 or (sample.get("throttle_pct") or 0) > 15
            or (sample.get("cpu_psi") or 0) > 25):
            reason = "cpu_pressure"
        elif sample.get("failure_rate", 0) > .1:
            reason = "failed_work"
        rate, tail = sample.get("success_per_s") or 0, sample.get("p95") or 0
        if sample.get("observations", 0) >= 3 and rate and tail:
            if self.previous_tail and tail > 1.5 * self.previous_tail and rate < .95 * self.previous_rate:
                reason = reason or "throughput_and_tail_regressed"
            self.previous_rate, self.previous_tail = rate, tail
        if reason:
            self.target = max(self.minimum, self.target // 2)
            self.next_growth = now + self.cooldown
        elif (now >= self.next_growth and sample.get("backlog", 0) > self.target
              and (self.resource != "cpu" or (self.cpu_ema is not None and self.cpu_ema < 65))):
            cap = self.maximum
            if self.resource == "cpu":
                cap = min(cap, max(1, math.ceil(sample.get("effective_cpus", 1))))
            self.target = min(cap, self.target + 1)
            self.next_growth = now + self.cooldown
            reason = "backlog_and_headroom"
        return self.target, reason or "hold"


class AdaptiveController:
    def __init__(self, db, processor, maximum, resource, *, work=None, branch=None, redis=None, interval=3):
        self.db, self.processor, self.work, self.branch = db, processor, work, branch
        self.redis, self.interval = redis, interval
        self.policy = CapacityPolicy(maximum, resource, cooldown=interval)
        self.monitor = ResourceMonitor()
        self.instance = str(uuid4())
        self.next_sample = 0.0

    def limit(self):
        now = time.monotonic()
        if now < self.next_sample:
            return self.policy.target
        self.next_sample = now + self.interval
        sample = self.monitor.sample(self.db, self.processor, self.work, self.branch, self.redis)
        previous = self.policy.target
        target, reason = self.policy.decide(sample, now)
        if target != previous:
            try:
                with self.db.uow() as repo:
                    repo.conn.execute(text("""INSERT INTO nf_scale_decisions
                        (processor,instance,work_id,previous,target,reason,sample)
                        VALUES(:group,:instance,:work,:previous,:target,:reason,CAST(:sample AS jsonb))"""),
                        {"group": self.processor, "instance": self.instance, "work": self.work, "previous": previous,
                         "target": target, "reason": reason, "sample": json.dumps(sample, allow_nan=False)})
                    repo.conn.execute(text("""DELETE FROM nf_scale_decisions WHERE id IN
                        (SELECT id FROM nf_scale_decisions WHERE created_at < now()-interval '7 days' ORDER BY id LIMIT 128)"""))
            except SQLAlchemyError:
                logging.getLogger(__name__).warning("Scaling decision persistence unavailable")
        return target
