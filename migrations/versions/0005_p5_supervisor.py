"""Server-side dispatch enrollment and immutable per-call Codex identity."""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_supervisor_jobs (
            task_id UUID PRIMARY KEY REFERENCES nf_tasks(task_id),
            config JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )
    """)
    op.execute("""
        CREATE TABLE nf_host_turns (
            attempt_id UUID PRIMARY KEY REFERENCES nf_call_attempts(attempt_id),
            thread_id TEXT NOT NULL UNIQUE,
            turn_id TEXT UNIQUE,
            phase TEXT NOT NULL CHECK (phase IN ('THREAD_STARTED','TURN_STARTED','COMPLETED')),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            CHECK ((turn_id IS NULL) = (phase = 'THREAD_STARTED'))
        )
    """)


def downgrade():
    # Retain the durable audit trail on an in-use deployment. A disposable
    # database or pre-deployment rollback has no host executions to discard.
    op.execute("LOCK TABLE nf_supervisor_jobs, nf_host_turns IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM nf_host_turns)
                  OR EXISTS (SELECT FROM nf_supervisor_jobs) THEN
                  RAISE EXCEPTION 'Preserve P5 enrollment and host evidence before rollback';
                  END IF; END $$""")
    op.drop_table("nf_host_turns")
    op.drop_table("nf_supervisor_jobs")
