"""The ordinary task commands shared by REST and a branch-bound MCP host."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract


@dataclass(frozen=True)
class TaskOperationsBinding:
    work_id: str
    branch_id: str

    def __post_init__(self):
        validate_contract("UUID", self.work_id)
        validate_contract("UUID", self.branch_id)

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

    def claim(self, worker_id, task_id=None, lease_seconds=30):
        kwargs = {"task_id": task_id, "lease_seconds": lease_seconds}
        if self.binding is not None:
            kwargs["scope"] = self.binding
        return self.kernel.claim_task(self.principal, worker_id, **kwargs)

    def result(self, result, worker_id):
        if self.binding is None:
            return self.kernel.submit_result(self.principal, result, worker_id)
        return self.kernel.submit_result(self.principal, result, worker_id, scope=self.binding)

    def cancel(self, task_id, generation, idempotency_key):
        if self.binding is None:
            return self.kernel.cancel_task(self.principal, task_id, generation, idempotency_key)
        return self.kernel.cancel_task(self.principal, task_id, generation, idempotency_key,
                                       scope=self.binding)
