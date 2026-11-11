"""Project transport fence and coalesced, replayable processor notifications."""
from alembic import op

revision = "0009_p55_processors"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_redis_branches (
            branch_id uuid PRIMARY KEY, work_id uuid NOT NULL,
            FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        )
    """)
    op.execute("""
        CREATE TABLE nf_processor_hints (
            branch_id uuid NOT NULL REFERENCES nf_redis_branches(branch_id) ON DELETE CASCADE,
            processor text NOT NULL CHECK(processor IN ('model','world','compute','recovery')),
            occurrence uuid NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            PRIMARY KEY(branch_id,processor)
        )
    """)
    op.execute("""CREATE TRIGGER nf_processor_wake AFTER INSERT OR UPDATE ON nf_processor_hints
                  FOR EACH STATEMENT EXECUTE FUNCTION nf_signal_supervisor()""")


def downgrade():
    op.execute("LOCK TABLE nf_redis_branches, nf_processor_hints IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM nf_redis_branches) THEN
        RAISE EXCEPTION 'Drain active processors and switch projects to postgres before downgrade';
        END IF; END $$""")
    op.drop_table("nf_processor_hints")
    op.drop_table("nf_redis_branches")
