"""Immutable proof of pre-send releases used to fence a route rebind."""
from alembic import op

revision = "0021_p58_routing"
down_revision = "0020_p57_harness_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_runtime_unsent (
          attempt_id uuid PRIMARY KEY REFERENCES nf_call_attempts, generation bigint NOT NULL,
          recorded_at timestamptz NOT NULL DEFAULT clock_timestamp());
        CREATE TRIGGER immutable_runtime_unsent BEFORE UPDATE OR DELETE ON nf_runtime_unsent
          FOR EACH ROW EXECUTE FUNCTION nf_reject_immutable_mutation();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_runtime_unsent IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_runtime_unsent) THEN
          RAISE EXCEPTION 'Preserve pre-send evidence before rollback'; END IF; END $$;
        DROP TABLE nf_runtime_unsent;
    """)
