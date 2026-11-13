"""Optional global sublimits for model and index execution under existing admission leases."""
from alembic import op

revision = "0024_hera_resources"
down_revision = "0023_hera_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE nf_dispatch_limits
        DROP CONSTRAINT nf_dispatch_limits_scope_check,
        ADD CONSTRAINT nf_dispatch_limits_scope_check CHECK (
            scope IN ('GLOBAL','WORK','ROLE') OR
            (scope='RESOURCE' AND subject IN ('MODEL','CPU_COMPUTE')))""")


def downgrade():
    # Validate before removing the new vocabulary. Active resource policies
    # require explicit retirement, and every existing task and lease survives.
    op.execute("""ALTER TABLE nf_dispatch_limits
        DROP CONSTRAINT nf_dispatch_limits_scope_check,
        ADD CONSTRAINT nf_dispatch_limits_scope_check CHECK (scope IN ('GLOBAL','WORK','ROLE'))""")
