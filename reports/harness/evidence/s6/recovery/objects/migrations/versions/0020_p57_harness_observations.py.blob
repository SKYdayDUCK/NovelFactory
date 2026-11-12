"""Add nullable structured observation data and a work/time index for bounded reporting.

Prior audit rows keep NULL metadata. The artifact index scans the table under a
SHARE lock; provision a write-maintenance window. Rolls back transactionally.
"""
from alembic import op

revision = "0020_p57_harness_observations"
down_revision = "0019_p57_context_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE nf_audit_events ADD COLUMN harness_details jsonb")
    op.execute("CREATE INDEX nf_artifacts_work_time ON nf_artifacts(work_id, created_at, artifact_revision_id)")


def downgrade():
    op.execute("""
        LOCK TABLE nf_audit_events IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM nf_audit_events WHERE harness_details IS NOT NULL)
            THEN RAISE EXCEPTION 'Restore a backup to preserve recorded Harness observations'; END IF;
        END $$;
        DROP INDEX nf_artifacts_work_time;
        ALTER TABLE nf_audit_events DROP COLUMN harness_details;
    """)
