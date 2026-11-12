"""Manager-only immutable work/branch policy revisions."""
from alembic import op

revision = "w012"
down_revision = "w011"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE aw_model_policies (
          org_id text NOT NULL REFERENCES aw_organizations,
          installation_id text NOT NULL REFERENCES aw_installations,
          work_id uuid NOT NULL, branch_id uuid NOT NULL,
          revision bigint CHECK(revision > 0), rules jsonb NOT NULL, author_id text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(org_id,installation_id,work_id,branch_id,revision)
        )
    """)


def downgrade():
    raise RuntimeError("Restore the paired backup to retain approved model routing revisions")
