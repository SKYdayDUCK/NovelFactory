"""Transport-neutral operations on the existing owner-fenced task ledger."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, tuple_

from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract
from novel_factory.infrastructure.database import tables


@dataclass(frozen=True)
class TaskOperationsBinding:
    """Trusted, optional branch ceiling on an installation's ordinary task API."""
    work_id: str
    branch_id: str

    def check(self, record):
        require((self.work_id, self.branch_id) == (record["work_id"], record["branch_id"]),
                "VIEW_DENIED", "Task unavailable")


class TaskOperations:
    def __init__(self, kernel, principal, *, binding=None, fenced_status=False, coalescer=None):
        self.kernel = kernel
        self.principal = principal
        self.binding = binding
        self.fenced_status = fenced_status
        self.coalescer = coalescer

    def create(self, spec, idempotency_key):
        if self.binding is not None:
            self.binding.check(spec["world_ref"])
        return self.kernel.create_task(self.principal, spec, idempotency_key)

    def submit(self, spec, idempotency_key):
        receipt = self.create(spec, idempotency_key)
        return {**receipt, "status_url": f"/api/v1/tasks/{receipt['task_id']}",
                "status_tool": "nf_task_status", "poll_after_ms": 1000,
                "wall_deadline": spec["wall_deadline"]}

    def status(self, task_id):
        if self.coalescer is None:
            return self._status(task_id)
        # The instance belongs to one application/kernel and status contract. All
        # participants join before the leader rechecks grant, owner and generation.
        key = (self.principal, self.binding, task_id, self.fenced_status)
        return self.coalescer.read(key, lambda: self._status(task_id))

    def _status(self, task_id):
        read = self.kernel.get_task_with_locked_work if self.fenced_status else self.kernel.get_task
        task = read(self.principal, task_id)
        if self.binding is not None:
            self.binding.check(task)
        return self._wire_status(task)

    @staticmethod
    def _wire_status(task):
        # The SDK structured result must carry the same JSON values as REST.
        return {k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v
                for k, v in task.items()}

    def status_batch(self, work_id, branch_id, task_ids):
        require(isinstance(task_ids, list) and 1 <= len(task_ids) <= 32,
                "INVALID_ARGUMENT", "Supply 1 to 32 task IDs")
        work_id, branch_id = (str(UUID(validate_contract("UUID", value))) for value in (work_id, branch_id))
        ids = [str(UUID(validate_contract("UUID", value))) for value in task_ids]
        require(len(set(ids)) == len(ids), "INVALID_ARGUMENT", "Duplicate task ID")
        if self.binding is not None:
            self.binding.check({"work_id": work_id, "branch_id": branch_id})
        tasks, inputs = tables["tasks"], tables["task_inputs"]
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, self.principal, work_id, "READ", branch_id)
            # The generation join pins the WorldRef in the same MVCC statement.
            query = (select(tasks, inputs.c.spec["world_ref"].label("world_ref"))
                     .join(inputs, (tasks.c.task_id == inputs.c.task_id) &
                                   (tasks.c.generation == inputs.c.generation))
                     .where(tasks.c.task_id.in_(ids), tasks.c.work_id == work_id,
                            tasks.c.branch_id == branch_id, tasks.c.grant_id == self.principal.grant_id,
                            tasks.c.principal_id == self.principal.principal_id))
            rows = {row["task_id"]: row for row in repo.conn.execute(query).mappings()}
            require(len(rows) == len(ids), "VIEW_DENIED", "Task unavailable")
            return {"tasks": [self._wire_status(self.kernel._public_task(rows[i], rows[i]["world_ref"])) for i in ids]}

    def cancel(self, task_id, generation, idempotency_key):
        return self.kernel.cancel_task(self.principal, task_id, generation, idempotency_key,
                                       scope=self.binding)

    def events(self, task_id, cursor=None, limit=50):
        require(type(limit) is int and 1 <= limit <= 100, "INVALID_ARGUMENT", "Invalid event limit")
        with self.kernel.db.uow(write=False) as repo:
            task, _ = self.kernel._task_owner(repo, self.principal, task_id)
            if self.binding is not None:
                self.binding.check(task)
            outbox = tables["outbox"]
            scope = (outbox.c.aggregate_id == task_id, outbox.c.topic == "task.changed",
                     outbox.c.aggregate_type == "task")
            query = select(outbox.c.outbox_id, outbox.c.payload).where(*scope)
            if cursor is not None:
                validate_contract("UUID", cursor)
                anchor = repo.conn.execute(select(outbox.c.created_at, outbox.c.outbox_id)
                    .where(*scope, outbox.c.outbox_id == cursor)).one_or_none()
                require(anchor is not None, "INVALID_ARGUMENT", "Cursor does not belong to this task")
                query = query.where(tuple_(outbox.c.created_at, outbox.c.outbox_id) > tuple(anchor))
            rows = repo.conn.execute(query.order_by(outbox.c.created_at, outbox.c.outbox_id).limit(limit + 1)).all()
            events = [{"event_id": row.outbox_id, **{k: row.payload[k] for k in ("task_id", "generation", "state")}}
                      for row in rows[:limit]]
        return {"events": events, "cursor": events[-1]["event_id"] if events else cursor,
                "has_more": len(rows) > limit}
