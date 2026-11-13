"""Optional transactionally maintained task totals; authorization stays at the caller."""
from sqlalchemy import text

_TASK_STATES = frozenset({
    "BLOCKED", "READY", "RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW",
    "ACCEPTED", "STALE", "PAUSED", "CANCELLED", "FAILED",
})


def _valid_counts(counts):
    return isinstance(counts, dict) and counts.keys() <= _TASK_STATES and all(
        type(n) is int and n >= 0 for n in counts.values()
    )


def _maintenance_suspended(conn):
    return conn.scalar(text("""
        SELECT EXISTS (SELECT FROM pg_constraint
            WHERE conrelid='nf_hera_progress'::regclass AND conname='nf_hera_progress_empty')
    """))


def set_progress_maintenance(conn, *, suspended):
    """Operator-only switch. Change both protections in the caller's transaction."""
    # The backfill uses the same task lock. Also exclude a concurrent direct
    # INSERT of a projection before proving the table empty.
    conn.execute(text("LOCK TABLE nf_tasks IN SHARE ROW EXCLUSIVE MODE"))
    conn.execute(text("LOCK TABLE nf_hera_progress IN ACCESS EXCLUSIVE MODE"))
    if suspended == _maintenance_suspended(conn):
        return
    if suspended:
        if conn.scalar(text("SELECT EXISTS (SELECT FROM nf_hera_progress)")):
            raise ValueError("Disable all configured branch projections before suspending maintenance")
        conn.execute(text("ALTER TABLE nf_hera_progress ADD CONSTRAINT nf_hera_progress_empty CHECK (false)"))
        conn.execute(text("ALTER TABLE nf_tasks DISABLE TRIGGER nf_hera_progress_delta"))
    else:
        conn.execute(text("ALTER TABLE nf_tasks ENABLE TRIGGER nf_hera_progress_delta"))
        conn.execute(text("ALTER TABLE nf_hera_progress DROP CONSTRAINT nf_hera_progress_empty"))


def configure_progress(conn, work_id, branch_id, *, enabled):
    # Serialize the initial snapshot with concurrent task transitions. This is an
    # explicit operator operation, never performed on a reader's cold path.
    conn.execute(text("LOCK TABLE nf_tasks IN SHARE ROW EXCLUSIVE MODE"))
    params = {"w": work_id, "b": branch_id}
    if not enabled:
        conn.execute(text("DELETE FROM nf_hera_progress WHERE work_id=:w AND branch_id=:b"), params)
        return
    if _maintenance_suspended(conn):
        set_progress_maintenance(conn, suspended=False)
    conn.execute(text("""
        INSERT INTO nf_hera_progress(work_id, branch_id, counts)
        SELECT :w, :b, COALESCE(jsonb_object_agg(state, n), '{}'::jsonb)
        FROM (SELECT state, count(*) AS n FROM nf_tasks
              WHERE work_id=:w AND branch_id=:b GROUP BY state) s
        ON CONFLICT(work_id,branch_id) DO UPDATE SET counts=EXCLUDED.counts,
            watermark=nf_hera_progress.watermark+1
    """), params)


def projected_counts(conn, work_id, branch_id):
    # A READY task behind a quota timer becomes eligible merely through the
    # passage of time. Correct the indexed totals in the same statement snapshot.
    row = conn.execute(text("""
        SELECT p.counts, (SELECT count(*) FROM nf_quota_retry_deadlines d
            JOIN nf_tasks t USING (task_id,generation)
            WHERE d.retry_not_before > statement_timestamp() AND t.state='READY'
              AND t.work_id=p.work_id AND t.branch_id=p.branch_id) AS deferred
        FROM nf_hera_progress p WHERE p.work_id=:w AND p.branch_id=:b
    """), {"w": work_id, "b": branch_id}).one_or_none()
    if row is None:
        return None
    counts = row.counts
    if not _valid_counts(counts):
        return None
    if row.deferred > counts.get("READY", 0):
        return None
    if row.deferred:
        counts["READY"] = counts.get("READY", 0) - row.deferred
        counts["BLOCKED"] = counts.get("BLOCKED", 0) + row.deferred
    return [(state, n) for state, n in counts.items() if n > 0]


def check_progress(conn, work_id, branch_id):
    """Compare the optional aggregate with the authoritative states in one snapshot."""
    row = conn.execute(text("""
        SELECT p.counts,
               (SELECT COALESCE(jsonb_object_agg(state, n), '{}'::jsonb)
                FROM (SELECT state, count(*) AS n FROM nf_tasks
                      WHERE work_id=p.work_id AND branch_id=p.branch_id GROUP BY state) live) AS expected
        FROM nf_hera_progress p WHERE work_id=:w AND branch_id=:b
    """), {"w": work_id, "b": branch_id}).one_or_none()
    if row is None:
        return "DISABLED"
    if not _valid_counts(row.counts):
        return "MISMATCH"
    actual = {state: n for state, n in row.counts.items() if n}
    return "OK" if actual == row.expected else "MISMATCH"
