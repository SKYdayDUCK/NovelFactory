"""Bounded, event-loop-local experimental DB scheduler. No network entry point."""
import asyncio
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import math

from sqlalchemy.exc import DBAPIError

from novel_factory.domain.errors import DomainError
from .operations import LeaseRenewal, TaskStatus, execute


@dataclass
class Ticket:
    operation: TaskStatus | LeaseRenewal
    future: asyncio.Future
    queued: float
    deadline: float
    started: bool = False


class Coordinator:
    def __init__(self, kernel, *, workers=4, capacity=128, batch=32, window_ms=2,
                 split_queues=False, join_input=False):
        if not 1 <= workers <= kernel.db.engine.pool.size() or not 1 <= capacity:
            raise ValueError("Invalid queue or database connection budget")
        if not 1 <= batch <= 128 or not 0 <= window_ms <= 10:
            raise ValueError("Invalid microbatch configuration")
        self.kernel = kernel
        self.workers = workers
        self.capacity = capacity
        self.batch = batch
        self.window = window_ms / 1000
        self.split_queues = split_queues
        self.join_input = join_input
        self.queues = [deque(), deque()] if split_queues else [deque()]
        self.cursor = 0
        self.event = asyncio.Event()
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.running = []
        self.closed = False
        self.counts = Counter()
        self.max_queue_wait_s = 0.0
        self.batches = Counter()

    async def __aenter__(self):
        self.running = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        return self

    async def __aexit__(self, *_):
        self.closed = True
        self.event.set()
        try:
            await asyncio.gather(*self.running)
        finally:
            self.pool.shutdown(wait=True)

    async def submit(self, operation, *, timeout=15.0):
        if type(operation) not in (TaskStatus, LeaseRenewal):
            raise TypeError("Unknown database operation")
        operation.validate()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        if self.closed or not self.running:
            raise DomainError("UNAVAILABLE", "Coordinator is closed")
        depth = sum(map(len, self.queues))
        if depth >= self.capacity:
            self.counts["rejected"] += 1
            raise DomainError("BACKPRESSURE", "Database queue is full", retryable=True)
        loop = asyncio.get_running_loop()
        now = loop.time()
        ticket = Ticket(operation, loop.create_future(), now, now + timeout)
        lane = int(self.split_queues and type(operation) is LeaseRenewal)
        self.queues[lane].append(ticket)
        self.counts["submitted"] += 1
        self.counts["peak_queued"] = max(self.counts["peak_queued"], depth + 1)
        self.event.set()
        try:
            async with asyncio.timeout_at(ticket.deadline):
                return await ticket.future
        except TimeoutError:
            self.counts["timed_out"] += 1
            if ticket.started and type(operation) is LeaseRenewal:
                raise DomainError("OUTCOME_UNKNOWN", "Check the current lease before resubmission") from None
            raise
        except asyncio.CancelledError:
            self.counts["cancelled"] += 1
            # Queued tickets are discarded. An already dispatched transaction
            # commits or rolls back normally; disconnect never retries a write.
            raise

    def _next_queue(self):
        for offset in range(len(self.queues)):
            index = (self.cursor + offset) % len(self.queues)
            if self.queues[index]:
                self.cursor = (index + 1) % len(self.queues)
                return self.queues[index]
        return None

    def _take_reads(self, queue, tickets):
        while len(tickets) < self.batch and queue and type(queue[0].operation) is TaskStatus:
            tickets.append(queue.popleft())

    async def _worker(self):
        loop = asyncio.get_running_loop()
        while True:
            queue = self._next_queue()
            if queue is None:
                if self.closed:
                    return
                self.event.clear()
                await self.event.wait()
                continue
            tickets = [queue.popleft()]
            if type(tickets[0].operation) is TaskStatus:
                self._take_reads(queue, tickets)
                # Sparse arrivals pass immediately. A partially filled batch
                # under pressure may collect more for the remaining latency budget.
                if 1 < len(tickets) < self.batch and not queue and not self.closed:
                    until = min(t.deadline for t in tickets)
                    until = min(until, tickets[0].queued + self.window)
                    delay = until - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._take_reads(queue, tickets)
            active = []
            for ticket in tickets:
                if ticket.future.done():
                    continue
                now = loop.time()
                if now >= ticket.deadline:
                    ticket.future.set_exception(TimeoutError())
                    continue
                self.max_queue_wait_s = max(self.max_queue_wait_s, now - ticket.queued)
                ticket.started = True
                active.append(ticket)
            if not active:
                continue
            operations = [ticket.operation for ticket in active]
            self.batches[len(active)] += 1
            self.counts["executing"] += 1
            self.counts["peak_executing"] = max(self.counts["peak_executing"], self.counts["executing"])
            try:
                # With size one the joined hybrid still uses its set-wise SQL
                # on one row; changing a queue limit must not change the query.
                call = partial(execute, self.kernel, operations, join_input=self.join_input,
                               grouped=self.batch > 1 or self.join_input)
                results = await loop.run_in_executor(self.pool, call)
                if len(results) != len(active):
                    raise RuntimeError("Incomplete batch response")
            except DBAPIError:
                self.counts["db_errors"] += 1
                if type(operations[0]) is LeaseRenewal:
                    results = [DomainError("OUTCOME_UNKNOWN", "Check the current lease before resubmission")]
                else:
                    results = [DomainError("UNAVAILABLE", "Database read unavailable") for _ in active]
            except Exception as error:
                # An infrastructure failure rolls back the read batch and no
                # item receives data. Never re-run a write with unknown outcome.
                results = [error] * len(active)
            finally:
                self.counts["executing"] -= 1
            for ticket, result in zip(active, results, strict=True):
                if ticket.future.done():
                    continue
                if loop.time() >= ticket.deadline:
                    ticket.future.set_exception(TimeoutError())
                elif isinstance(result, Exception):
                    ticket.future.set_exception(result)
                else:
                    ticket.future.set_result(result)
