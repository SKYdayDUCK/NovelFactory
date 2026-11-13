"""The ordinary task commands shared by REST and a branch-bound MCP host."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from novel_factory.application.kernel import require


@dataclass(frozen=True)
class TaskOperationsBinding:
    work_id: str
    branch_id: str

    def check(self, row):
        require((self.work_id, self.branch_id) == (row["work_id"], row["branch_id"]),
                "VIEW_DENIED", "Task unavailable")


class TaskOperations:
    def __init__(self, kernel, principal, binding=None):
        self.kernel = kernel
        self.principal = principal
        self.binding = binding

    def create(self, spec, idempotency_key):
        if self.binding is not None:
            self.binding.check(spec["world_ref"])
        return self.kernel.create_task(self.principal, spec, idempotency_key)

    def status(self, task_id):
        task = self.kernel.get_task(self.principal, task_id)
        if self.binding is not None:
            self.binding.check(task)
        return {key: value.isoformat() if isinstance(value, datetime) else
                str(value) if isinstance(value, Decimal) else value for key, value in task.items()}

    def cancel(self, task_id, generation, idempotency_key):
        if self.binding is None:
            return self.kernel.cancel_task(self.principal, task_id, generation, idempotency_key)
        return self.kernel.cancel_task(self.principal, task_id, generation, idempotency_key,
                                       scope=self.binding)
