"""Bounded asynchronous admission before acquiring a database connection.

Budgets are per API process. The deployment budget validates their aggregate;
database transactions/leases remain the cross-process authority. No model work
is performed while an admission slot or database transaction is held.
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time
from typing import Callable

from novel_factory.domain.errors import DomainError


@dataclass(frozen=True)
class ConnectionBudget:
    api_processes: int = 1
    api_pool_size: int = 8
    api_pool_overflow: int = 0
    worker_processes: int = 1
    worker_pool_size: int = 4
    maintenance_reserve: int = 16
    postgres_max_connections: int = 60
    active_stdio_processes: int = 0
    compute_child_processes: int = 0
    listen_connections: int = 0

    @property
    def total(self) -> int:
        return (self.api_processes * (self.api_pool_size + self.api_pool_overflow)
                + self.worker_processes * self.worker_pool_size + self.maintenance_reserve
                + 2 * self.active_stdio_processes + self.compute_child_processes + self.listen_connections)

    def validate(self) -> None:
        values = (self.api_processes, self.api_pool_size, self.worker_pool_size,
                  self.postgres_max_connections)
        if any(value < 1 for value in values):
            raise ValueError("process and pool sizes must be positive")
        if min(self.api_pool_overflow, self.worker_processes, self.maintenance_reserve,
               self.active_stdio_processes, self.compute_child_processes, self.listen_connections) < 0:
            raise ValueError("connection budgets cannot be negative")
        if self.total > self.postgres_max_connections:
            raise ValueError("aggregate configured connection budget exceeds PostgreSQL limit")


@dataclass(eq=False)
class _Ticket:
    key: str
    kind: str
    future: asyncio.Future
    enqueued_at: float
    granted: bool = False
    released: bool = False


class AdmissionController:
    """Round-robin per-principal queue with bounded total and category slots."""

    def __init__(self, capacity: int, *, per_principal: int = 2, max_waiters: int = 64,
                 max_waiters_per_principal: int = 8, timeout: float = 2.0,
                 class_limits: dict[str, int] | None = None, fast_slots: int = 0):
        if capacity < 1 or per_principal < 1 or max_waiters < 0 or max_waiters_per_principal < 0 or timeout <= 0:
            raise ValueError("invalid admission budget")
        if class_limits and any(limit < 1 or limit > capacity for limit in class_limits.values()):
            raise ValueError("invalid category budget")
        if type(fast_slots) is not int or not 0 <= fast_slots < capacity:
            raise ValueError("fast slots must leave at least one shared execution slot")
        self.fast_slots = fast_slots
        self.capacity = capacity
        self.per_principal = per_principal
        self.max_waiters = max_waiters
        self.max_waiters_per_principal = max_waiters_per_principal
        self.timeout = timeout
        self.class_limits = class_limits or {}
        self._lock = asyncio.Lock()
        self._queues: dict[str, deque[_Ticket]] = {}
        self._rotation: deque[str] = deque()
        self._active_by_key: Counter[str] = Counter()
        self._active_by_kind: Counter[str] = Counter()
        self._active = 0
        self._waiting = 0
        self._stats: Counter[str] = Counter()
        self._wait_seconds = 0.0

    def snapshot(self) -> dict:
        return {**dict(self._stats), "active": self._active, "queued": self._waiting,
                "capacity": self.capacity, "per_principal": self.per_principal,
                "max_waiters": self.max_waiters, "wait_seconds_total": self._wait_seconds}

    def _available(self, ticket: _Ticket) -> bool:
        return (self._active < self.capacity
                and (not self.fast_slots or ticket.kind == "fast"
                     or self._active - self._active_by_kind["fast"] < self.capacity - self.fast_slots)
                and self._active_by_key[ticket.key] < self.per_principal
                and self._active_by_kind[ticket.kind] < self.class_limits.get(ticket.kind, self.capacity))

    def _dispatch(self) -> None:
        while self._active < self.capacity and self._rotation:
            selected = None
            for _ in range(len(self._rotation)):
                key = self._rotation[0]
                self._rotation.rotate(-1)
                queue = self._queues[key]
                if self._available(queue[0]):
                    selected = queue.popleft()
                elif self.fast_slots:
                    selected = next((t for t in queue if t.kind == "fast" and self._available(t)), None)
                    if selected is not None:
                        queue.remove(selected)
                if selected is not None:
                    if not queue:
                        del self._queues[key]
                        self._rotation.remove(key)
                    break
            if selected is None:
                break
            self._waiting -= 1
            self._active += 1
            self._active_by_key[selected.key] += 1
            self._active_by_kind[selected.kind] += 1
            self._stats["admitted"] += 1
            self._stats["peak_active"] = max(self._stats["peak_active"], self._active)
            self._wait_seconds += time.monotonic() - selected.enqueued_at
            selected.granted = True
            selected.future.set_result(None)

    def _release(self, ticket: _Ticket) -> None:
        if ticket.released:
            return
        ticket.released = True
        if ticket.granted:
            self._active -= 1
            self._active_by_key[ticket.key] -= 1
            self._active_by_kind[ticket.kind] -= 1
            if not self._active_by_key[ticket.key]:
                del self._active_by_key[ticket.key]
            if not self._active_by_kind[ticket.kind]:
                del self._active_by_kind[ticket.kind]
        else:
            queue = self._queues.get(ticket.key)
            if queue and ticket in queue:
                queue.remove(ticket)
                self._waiting -= 1
                if not queue:
                    del self._queues[ticket.key]
                    self._rotation.remove(ticket.key)
        self._dispatch()

    @asynccontextmanager
    async def slot(self, key: str, kind: str = "default"):
        ticket = _Ticket(key, kind, asyncio.get_running_loop().create_future(), time.monotonic())
        async with self._lock:
            immediate = not self._rotation and self._available(ticket)
            if not immediate and (self._waiting >= self.max_waiters
                    or len(self._queues.get(key, ())) >= self.max_waiters_per_principal):
                self._stats["rejected"] += 1
                raise DomainError("BACKPRESSURE", "Request capacity is full", True, "WAIT")
            if key not in self._queues:
                self._queues[key] = deque()
                self._rotation.append(key)
            self._queues[key].append(ticket)
            self._waiting += 1
            self._dispatch()
            self._stats["peak_queued"] = max(self._stats["peak_queued"], self._waiting)
        try:
            await asyncio.wait_for(asyncio.shield(ticket.future), timeout=self.timeout)
        except (asyncio.CancelledError, TimeoutError) as exc:
            async with self._lock:
                self._stats["cancelled" if isinstance(exc, asyncio.CancelledError) else "timed_out"] += 1
                self._release(ticket)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DomainError("BACKPRESSURE", "Request queue wait expired", True, "WAIT") from None
        try:
            yield
        finally:
            async with self._lock:
                self._stats["completed"] += 1
                self._release(ticket)


class TokenBucketLimiter:
    """Authenticated-principal rate limit, independent of request concurrency."""

    def __init__(self, rate: float = 20, burst: int = 100, *, clock: Callable[[], float] = time.monotonic,
                 maximum_principals: int = 4096):
        if rate <= 0 or burst < 1 or maximum_principals < 1:
            raise ValueError("invalid rate budget")
        self.rate, self.burst, self.clock = rate, burst, clock
        self.maximum_principals = maximum_principals
        self._states: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, principal_id: str) -> None:
        async with self._lock:
            now = self.clock()
            if principal_id not in self._states and len(self._states) >= self.maximum_principals:
                # A bucket is disposable only when it has fully refilled.
                self._states = {key: state for key, state in self._states.items()
                                if state[0] + max(0, now - state[1]) * self.rate < self.burst}
                if len(self._states) >= self.maximum_principals:
                    raise DomainError("BACKPRESSURE", "Admission identity capacity is full", True, "WAIT")
            tokens, updated = self._states.get(principal_id, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + max(0.0, now - updated) * self.rate)
            if tokens < 1:
                self._states[principal_id] = (tokens, now)
                raise DomainError("RATE_LIMITED", "Request rate exceeded", True, "WAIT")
            self._states[principal_id] = (tokens - 1, now)
