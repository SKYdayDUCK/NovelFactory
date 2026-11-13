"""Optional transactionally maintained task totals; authorization stays at the caller."""
from sqlalchemy import text


def configure_progress(conn, work_id, branch_id, *, enabled):
    # Serialize the initial snapshot with concurrent task transitions. This is an
    # explicit operator operation, never performed on a reader's cold path.
    conn.execute(text("LOCK TABLE nf_tasks IN SHARE ROW EXCLUSIVE MODE"))
    params = {"w": work_id, "b": branch_id}
    if not enabled:
        conn.execute(text("DELETE FROM nf_hera_progress WHERE work_id=:w AND branch_id=:b"), params)
        return
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
    counts = dict(row.counts)
    if row.deferred:
        counts["READY"] = counts.get("READY", 0) - row.deferred
        counts["BLOCKED"] = counts.get("BLOCKED", 0) + row.deferred
    return [(state, n) for state, n in counts.items() if n > 0]
