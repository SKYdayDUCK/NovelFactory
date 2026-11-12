"""Optional latency- and backlog-sensitive FIFO status coalescing experiment."""
import asyncio

from .coordinator import Coordinator
from .operations import TaskStatus


class AdaptiveCoordinator(Coordinator):
    def __init__(self, kernel, **settings):
        super().__init__(kernel, **settings)
        if self.join_input or self.split_queues:
            raise ValueError("Adaptive ablation compares the same FIFO and SQL as micro")
        self.slots = asyncio.BoundedSemaphore(self.workers)
        self.direct_tasks = set()
        self.collecting = self.waiting_for_slot = 0
        self.read_service_s = None

    def _enqueue(self, ticket, depth):
        direct_active = any(not task.done() for task in self.direct_tasks)
        idle = not (depth or direct_active or self.collecting or
                    self.waiting_for_slot or self.counts["executing"])
        if not idle or type(ticket.operation) is not TaskStatus:
            return super()._enqueue(ticket, depth)
        # An idle status read calls the incumbent singleton through the shared
        # executor immediately. It is never enqueued or given a fill deadline.
        self.counts["direct_status"] += 1
        task = asyncio.create_task(self._dispatch([ticket], asyncio.get_running_loop(), grouped=False))
        self.direct_tasks.add(task)
        task.add_done_callback(self.direct_tasks.discard)

    async def _collect(self, queue, loop):
        tickets = [queue.popleft()]
        if type(tickets[0].operation) is not TaskStatus:
            return tickets
        self.collecting += 1
        try:
            self._take_reads(queue, tickets)
            depth = len(tickets)
            if depth == 1 or depth == self.batch or queue:
                self.counts["fifo_immediate"] += 1
                return tickets
            # Use only observed service time. A cold coordinator has no
            # evidence that additional fill delay is worthwhile.
            elapsed = loop.time() - tickets[0].queued
            budget = min(self.window, .25 * (self.read_service_s or 0))
            target = min(self.batch, 1 << (depth - 1).bit_length())
            if depth == target or elapsed >= budget:
                self.counts["fifo_immediate"] += 1
                return tickets
            until = min(tickets[0].queued + budget, min(t.deadline for t in tickets))
            delay = until - loop.time()
            if delay > 0:
                self.counts["feedback_wait"] += 1
                await asyncio.sleep(delay)
                while len(tickets) < target and queue and type(queue[0].operation) is TaskStatus:
                    tickets.append(queue.popleft())
            return tickets
        finally:
            self.collecting -= 1

    async def _dispatch(self, tickets, loop, *, grouped):
        self.waiting_for_slot += 1
        try:
            await self.slots.acquire()
        finally:
            self.waiting_for_slot -= 1
        start = loop.time()
        try:
            await super()._dispatch(tickets, loop, grouped=grouped)
        finally:
            if type(tickets[0].operation) is TaskStatus and any(t.started for t in tickets):
                duration = loop.time() - start
                self.read_service_s = (duration if self.read_service_s is None
                                       else self.read_service_s * .75 + duration * .25)
                self.counts["read_ewma_us"] = round(self.read_service_s * 1_000_000)
            self.slots.release()

    async def __aexit__(self, *args):
        self.closed = True
        if self.direct_tasks:
            await asyncio.gather(*self.direct_tasks)
        await super().__aexit__(*args)
