"""Sparse deadlines for quota retries; old tasks and immutable inputs are untouched."""
from alembic import op

revision = "0022_quota_retry"
down_revision = "0021_p58_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_quota_retry_deadlines (
            task_id uuid PRIMARY KEY,
            generation integer NOT NULL,
            attempt_id uuid NOT NULL REFERENCES nf_runtime_unsent(attempt_id),
            retry_not_before timestamptz NOT NULL,
            FOREIGN KEY (task_id,generation) REFERENCES nf_task_inputs(task_id,generation)
        );
        CREATE INDEX ix_nf_quota_retry_deadline ON nf_quota_retry_deadlines(retry_not_before)
    """)


def downgrade():
    # Outstanding cooldowns still protect a generation. Historical proof of
    # every actual refund is retained separately in the immutable unsent ledger.
    op.execute("LOCK TABLE nf_quota_retry_deadlines IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT FROM nf_quota_retry_deadlines d JOIN nf_tasks t USING(task_id,generation)
                     WHERE t.state='READY' AND d.retry_not_before > statement_timestamp()) THEN
            RAISE EXCEPTION 'Drain active quota retry timers before rollback';
          END IF;
        END $$;
        DROP TABLE nf_quota_retry_deadlines;
    """)
