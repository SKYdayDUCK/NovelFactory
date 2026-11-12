"""Admission for live, project-bound supervisors. No controller may claim for another."""
from sqlalchemy import text

from novel_factory.orchestration.tasks import MAINTENANCE_GATE

_NEXT = text("""
    WITH active AS MATERIALIZED (
        SELECT t.work_id, t.role_id FROM nf_dispatch_slots s
        JOIN nf_tasks t USING(task_id) WHERE s.expires_at > statement_timestamp()
        UNION ALL SELECT work_id, 'derived_index' AS role_id FROM nf_index_leases WHERE expires_at > statement_timestamp()
    ), next_job AS (
        SELECT t.task_id, t.branch_id
        FROM nf_dispatch_waiters q
        JOIN nf_tasks t ON q.branch_id=t.branch_id
        JOIN nf_task_fairness f USING(work_id, task_kind)
        JOIN nf_supervisor_jobs j USING(task_id)
        JOIN nf_grants g ON g.grant_id=t.grant_id
        JOIN nf_works w ON w.work_id=t.work_id
        WHERE q.valid_until > statement_timestamp() AND t.state='READY'
          AND t.deadline > statement_timestamp() AND g.active AND g.work_id=t.work_id
          AND g.principal_id::text=t.principal_id AND g.role_id=t.role_id
          AND g.epoch=(j.config->>'grant_epoch')::bigint AND w.policy_version=j.config->>'policy_version'
          AND (g.branch_ids='["*"]'::jsonb OR g.branch_ids @> jsonb_build_array(t.branch_id))
          AND g.capabilities @> '["TASK_CLAIM","READ","COMPILE"]'::jsonb
          AND NOT EXISTS(SELECT FROM nf_dispatch_slots s WHERE s.task_id=t.task_id
                         AND s.expires_at > statement_timestamp())
          AND NOT EXISTS(SELECT FROM nf_call_attempts c WHERE c.task_id=t.task_id
              AND NOT EXISTS(SELECT FROM nf_runtime_unsent u WHERE u.attempt_id=c.attempt_id))
          AND NOT EXISTS(SELECT FROM nf_conflict_cases sc WHERE sc.task_id=t.task_id
                         AND sc.generation=t.generation AND NOT EXISTS
                         (SELECT FROM nf_conflict_resolutions sr WHERE sr.case_id=sc.case_id
                          AND sr.outcome IN ('REVALIDATED','CERTIFICATE_REISSUED')))
          AND (SELECT count(*) FROM active) <
              (SELECT slots FROM nf_dispatch_limits WHERE scope='GLOBAL' AND subject='*')
          AND (SELECT count(*) FROM active a WHERE a.work_id=t.work_id) <
              (SELECT slots FROM nf_dispatch_limits WHERE scope='WORK' AND subject IN ('*',t.work_id::text)
               ORDER BY subject='*' LIMIT 1)
          AND (SELECT count(*) FROM active a WHERE a.role_id=t.role_id) <
              (SELECT slots FROM nf_dispatch_limits WHERE scope='ROLE' AND subject IN ('*',t.role_id)
               ORDER BY subject='*' LIMIT 1)
        ORDER BY q.requested_at, q.branch_id, f.last_served_at, t.created_at, t.task_id LIMIT 1
        FOR UPDATE OF t SKIP LOCKED FOR SHARE OF g,w SKIP LOCKED
    )
    SELECT task_id::text FROM next_job WHERE branch_id=:branch
""")


def redis_enabled(conn, work, branch):
    return conn.scalar(text("""SELECT EXISTS (SELECT FROM nf_redis_branches
        WHERE work_id=:work AND branch_id=:branch)"""), {"work": work, "branch": branch})


def next_job(conn, work, branch, *, transport="postgres"):
    """Install/refresh this branch's ticket and pick the oldest eligible live queue.

    All supervisors use the existing short queue gate. The caller must reserve
    the slot and commit the kernel claim in this same transaction, before I/O.
    """
    params = {"work": work, "branch": branch}
    conn.execute(text("SELECT pg_advisory_xact_lock(:gate)"), {"gate": MAINTENANCE_GATE + 1})
    if (transport == "redis") != redis_enabled(conn, work, branch):
        return None
    conn.execute(text("LOCK TABLE nf_dispatch_limits IN SHARE MODE"))
    conn.execute(text("DELETE FROM nf_dispatch_waiters WHERE valid_until <= statement_timestamp()"))
    # A live generation owns its slot through its current lease. Older servers
    # wrote only the initial expiry. For completed or superseded executions,
    # retain the original crash-cleanup deadline until the host releases it.
    conn.execute(text("""
        MERGE INTO nf_dispatch_slots AS slot
        USING (
            SELECT s.attempt_id,
                   CASE WHEN t.state='RUNNING' AND t.attempt_id=s.attempt_id
                        THEN t.lease_expiry ELSE s.expires_at END AS valid_until
            FROM nf_dispatch_slots s JOIN nf_tasks t USING(task_id)
        ) AS lease ON slot.attempt_id=lease.attempt_id
        WHEN MATCHED AND lease.valid_until <= statement_timestamp() THEN DELETE
        WHEN MATCHED AND slot.expires_at <> lease.valid_until THEN UPDATE SET expires_at=lease.valid_until
    """))
    conn.execute(text("""
        INSERT INTO nf_dispatch_waiters(branch_id,requested_at,valid_until)
        SELECT :branch, statement_timestamp(), statement_timestamp() + interval '2 minutes'
        WHERE EXISTS (SELECT FROM nf_tasks t JOIN nf_supervisor_jobs j USING(task_id)
                      WHERE t.work_id=:work AND t.branch_id=:branch AND t.state='READY')
        ON CONFLICT(branch_id) DO UPDATE SET valid_until=EXCLUDED.valid_until
    """), params)
    return conn.scalar(_NEXT, params)


def reserve(repo, claimed):
    repo.add("dispatch_slots", {"attempt_id": claimed["attempt_id"], "task_id": claimed["task_id"],
                                "expires_at": claimed["lease_expiry"]})
    repo.conn.execute(text("DELETE FROM nf_dispatch_waiters WHERE branch_id=:branch"),
                      {"branch": claimed["branch_id"]})
