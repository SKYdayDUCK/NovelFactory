"""Bound duplicate status reads whose individual call intervals overlap the same DB read."""
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Condition
from time import monotonic


@dataclass
class _Group:
    future: Future = field(default_factory=Future)
    members: int = 1


class StatusCoalescer:
    def __init__(self, *, window=0.001, max_groups=64, max_members=6):
        if not (0 < window <= 0.01 and 0 < max_groups <= 1024 and 2 <= max_members <= 32):
            raise ValueError("invalid status coalescing bounds")
        self.window, self.max_groups, self.max_members = window, max_groups, max_members
        self._condition = Condition()
        self._collecting = {}
        self.joined = 0
        self.bypassed = 0

    def read(self, key, operation):
        with self._condition:
            group = self._collecting.get(key)
            if group is not None:
                group.members += 1
                self.joined += 1
                if group.members == self.max_members:
                    del self._collecting[key]
                    self._condition.notify_all()
                leader = False
            elif len(self._collecting) >= self.max_groups:
                self.bypassed += 1
                leader = False
            else:
                group = _Group()
                self._collecting[key] = group
                leader = True
                deadline = monotonic() + self.window
                self._condition.wait_for(lambda: self._collecting.get(key) is not group,
                                         timeout=max(0, deadline - monotonic()))
                # Never let a request that arrives after the database read began
                # inherit a pre-revocation or pre-rebinding result.
                if self._collecting.get(key) is group:
                    del self._collecting[key]
        if group is None:
            return operation()
        if not leader:
            return group.future.result()
        try:
            result = operation()
        except BaseException as error:
            group.future.set_exception(error)
            raise
        group.future.set_result(result)
        return result
