"""Durable admission in the same transaction as an authorized TaskService create.

The scan path serializes admissions on the policy. The exact path enforces limits
inside the task INSERT, sharing the configuration lock across clients.
Acceptance, cancellation, execution and model accounting retain their own guards.
All nonterminal tasks, including tasks created before activation, consume capacity.
"""
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from novel_factory.domain.errors import DomainError


_SHARED_POLICY = text("""
    SELECT enabled, exact_counters, max_age_seconds, max_queue_seconds, role_resources,
           clock_timestamp() AS db_now
    FROM nf_durable_admission_policy WHERE singleton AND (NOT enabled OR exact_counters) FOR SHARE
""")
_LOCK_POLICY = text("""
    UPDATE nf_durable_admission_policy SET enabled=enabled WHERE singleton AND enabled AND NOT exact_counters
    RETURNING enabled, exact_counters, clock_timestamp() AS db_now, total_limit, work_limit, principal_limit, resource_limit,
        max_age_seconds, max_queue_seconds, role_resources
""")
_OCCUPANCY = text("""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE t.work_id=:work) AS work,
           count(*) FILTER (WHERE t.principal_id=:principal) AS principal,
           count(*) FILTER (
             WHERE coalesce(a.resource_class, p.role_resources->>t.role_id, 'MODEL')=:resource
           ) AS resource
    FROM nf_tasks t CROSS JOIN nf_durable_admission_policy p
    LEFT JOIN nf_durable_admissions a ON a.task_id=t.task_id
    WHERE p.singleton AND t.state NOT IN ('ACCEPTED','FAILED','CANCELLED','STALE')
""")


@contextmanager
def translate_capacity_error():
    """Translate only the database quota guard; callers own the transaction."""
    try:
        yield
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "NF001":
            raise
        raise DomainError("BACKPRESSURE", "Durable task capacity is full; retry after one second",
                          True, "WAIT") from exc


def admit(conn, spec, principal_id, deadline):
    policy = conn.execute(_SHARED_POLICY).mappings().one_or_none()
    if policy is None:
        policy = conn.execute(_LOCK_POLICY).mappings().one_or_none()
        if policy is None:
            policy = conn.execute(_SHARED_POLICY).mappings().one_or_none()
        if policy is None:
            raise DomainError("SERVICE_UNAVAILABLE", "Admission policy changed; retry", True, "WAIT")
    if not policy["enabled"]:
        return None
    now = policy["db_now"]
    if deadline <= now:
        raise DomainError("DEADLINE_EXCEEDED", "Task deadline has passed")
    if deadline - now > timedelta(seconds=policy["max_age_seconds"]):
        raise DomainError("INVALID_ARGUMENT", "Task deadline exceeds the configured maximum age")
    # Kernel already matched the task role against the caller's locked grant.
    # The resource mapping is trusted database configuration; clients supply no class.
    resource = policy["role_resources"].get(spec["role_id"], "MODEL")
    if not policy["exact_counters"]:
        occupancy = conn.execute(_OCCUPANCY, {
            "work": spec["world_ref"]["work_id"], "principal": str(principal_id), "resource": resource,
        }).mappings().one()
        if any(occupancy[scope] >= policy[scope + "_limit"] for scope in occupancy):
            raise DomainError("BACKPRESSURE", "Durable task capacity is full; retry after one second", True, "WAIT")
    return {"resource_class": resource,
            "queue_deadline": min(deadline, now + timedelta(seconds=policy["max_queue_seconds"]))}
