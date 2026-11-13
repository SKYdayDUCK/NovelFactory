"""Demand-created execution resources; durable task/Agent state stays in PostgreSQL."""

import math
import time
from concurrent.futures import BrokenExecutor
from threading import RLock


class AgentLifecycleManager:
    """Create a pool on submit and retire it only after all admitted calls finish.

    This ordinary program neither selects tasks nor interprets model outputs.
    Retiring or rebuilding a pool never retries a submitted operation.
    """

    def __init__(self, pool_factory, *, idle_seconds=45, clock=time.monotonic, observe=None):
        if not math.isfinite(idle_seconds) or idle_seconds < 0:
            raise ValueError("Invalid worker idle interval")
        self._factory, self._clock, self._observe = pool_factory, clock, observe
        self.idle_seconds = idle_seconds
        self._lock = RLock()
        self._pool, self._futures = None, set()
        self._idle_since = None
        self._closed = self._broken = False
        self.starts = self.stops = 0

    def _emit(self, status):
        if self._observe:
            self._observe({"status": status, "starts": self.starts, "stops": self.stops})

    def submit(self, function, /, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("Execution manager is shut down")
            if self._pool is None:
                self._pool = self._factory()
                self._broken = False
                self.starts += 1
                self._emit("WORKER_POOL_STARTED")
            try:
                future = self._pool.submit(function, *args, **kwargs)
            except BrokenExecutor:
                self._broken = True
                raise
            self._futures.add(future)
            self._idle_since = None
            future.add_done_callback(self._completed)
            return future

    def _completed(self, future):
        with self._lock:
            self._futures.discard(future)
            if not future.cancelled() and isinstance(future.exception(), BrokenExecutor):
                self._broken = True
            if not self._futures:
                self._idle_since = self._clock()

    @property
    def state(self):
        with self._lock:
            if self._closed:
                return "DRAINING" if self._futures else "STOPPED"
            if self._futures:
                return "ACTIVE"
            return "WAITING" if self._pool is not None else "DORMANT"

    def idle_remaining(self):
        with self._lock:
            if self._futures or self._pool is None:
                return None
            if self._broken:
                return 0.0
            return max(0.0, self.idle_seconds - (self._clock() - self._idle_since))

    def maintain(self):
        """Called by the event loop, including after database/listener failure."""
        with self._lock:
            if self._pool is None or self._futures or self.idle_remaining() != 0:
                return False
            pool, self._pool = self._pool, None
            self._idle_since = None
        # Completed callbacks acquire the same lock; never join under it.
        pool.shutdown(wait=True)
        self.stops += 1
        self._emit("WORKER_POOL_RETIRED")
        return True

    def shutdown(self, wait=True):
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=wait)
            self.stops += 1
            self._emit("WORKER_POOL_STOPPED")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.shutdown()
