"""Internal typed operations for the isolated task-status and lease experiment."""
from dataclasses import dataclass
import re
from uuid import UUID

from sqlalchemy import select, tuple_

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.domain.errors import DomainError


@dataclass(frozen=True)
class TaskStatus:
    principal: Principal
    task_id: str

    def validate(self):
        require(isinstance(self.principal, Principal), "VIEW_DENIED", "Authenticated identity required")
        for key in (self.task_id, self.principal.grant_id):
            database_uuid(key)


@dataclass(frozen=True)
class LeaseRenewal:
    """Trusted supervisor input. This operation never joins a read transaction."""
    task_id: str
    generation: int
    worker_id: str
    lease_seconds: int = 30

    def validate(self):
        database_uuid(self.task_id)
        require(isinstance(self.generation, int) and self.generation >= 0 and
                isinstance(self.worker_id, str) and self.worker_id and
                type(self.lease_seconds) is int and 0 < self.lease_seconds <= 3600,
                "INVALID_ARGUMENT", "Invalid lease binding")


_PG_UUID = re.compile(r"[0-9a-fA-F]{4}(?:-?[0-9a-fA-F]{4}){7}")


def database_uuid(value):
    """Accept PostgreSQL UUID spellings. Only normalize DB keys, never Principal identity."""
    if isinstance(value, UUID):
        return str(value)
    require(isinstance(value, str), "INVALID_ARGUMENT", "Invalid bound identity")
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    require(_PG_UUID.fullmatch(value), "INVALID_ARGUMENT", "Invalid bound identity")
    return str(UUID(value))


def read_statuses(kernel, requests, *, join_input):
    """Return one ordered result or safe domain error per prevalidated request.

    Keys never come from request-supplied tenant or branch claims. Lock all
    authoritative work rows first and then all grant rows in stable PK order.
    The same Kernel validators run on each item even when IDs repeat. No data
    or authorization result survives the transaction. Resume fences stay live.
    """
    inputs = kernel.tasks.tables["task_inputs"]
    task_keys = {op.task_id: database_uuid(op.task_id) for op in requests}
    grant_keys = {op.principal.grant_id: database_uuid(op.principal.grant_id) for op in requests}
    task_ids = sorted(set(task_keys.values()))
    with kernel.db.uow(write=False) as repo:
        if join_input:
            task_rows = repo.tasks_with_world_ref(task_ids)
        else:
            task_rows = repo.many("tasks", {"task_id": task_ids})
        rows = {row["task_id"]: row for row in task_rows}
        works = {row["work_id"]: row for row in repo.many(
            "works", {"work_id": sorted({row["work_id"] for row in rows.values()})}, lock="share", order=["work_id"])}
        grants = {row["grant_id"]: row for row in repo.many(
            "grants", {"grant_id": sorted({grant_keys[op.principal.grant_id] for op in requests if task_keys[op.task_id] in rows})},
            lock="share", order=["grant_id"])}
        if not join_input and rows:
            # An intervening renewal, cancellation or new generation cannot
            # change which immutable input corresponds to the sampled task.
            keys = [(row["task_id"], row["generation"]) for row in rows.values()]
            world_refs = repo.conn.execute(select(inputs.c.task_id, inputs.c.spec["world_ref"]).where(
                tuple_(inputs.c.task_id, inputs.c.generation).in_(keys)))
            for task_id, world_ref in world_refs:
                rows[task_id]["world_ref"] = world_ref
        results = []
        for op in requests:
            task = rows.get(task_keys[op.task_id])
            try:
                require(task is not None, "VIEW_DENIED", "Task unavailable")
                work = works.get(task["work_id"])
                kernel._validate_locked_grant(repo, op.principal, task["work_id"], "READ", task["branch_id"],
                                              work, grants.get(grant_keys[op.principal.grant_id]))
                kernel._require_task_owner(task, op.principal)
                require(task.get("world_ref") is not None, "INTERNAL_ERROR", "Task input unavailable")
                # world_ref is a flat JSON object. Give each caller its own copy.
                result = kernel._public_task(task, dict(task["world_ref"]))
            except DomainError as error:
                result = error
            results.append(result)
        return results


def execute(kernel, requests, *, join_input, grouped):
    """Called by the DB executor. This has no arbitrary SQL/callable variant."""
    if len(requests) == 1:
        op = requests[0]
        if type(op) is LeaseRenewal:
            return [kernel.tasks.renew(op.task_id, op.generation, op.worker_id, lease_seconds=op.lease_seconds)]
        if type(op) is TaskStatus and not grouped:
            return [kernel.get_task(op.principal, op.task_id)]
    if not all(type(op) is TaskStatus for op in requests) or not requests:
        raise ValueError("Only task-status observations can share a transaction")
    return read_statuses(kernel, requests, join_input=join_input)
