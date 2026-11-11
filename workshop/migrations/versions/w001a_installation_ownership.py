"""Bind organization authority to its provisioning installation and an explicit membership grant."""

from alembic import op

revision = "w001a"
down_revision = "w001"
branch_labels = None
depends_on = None


def upgrade():
    # Neither the w000 token nor the w001 directory proves installation ownership.
    # Preserve existing rows; the operator claims the directory and issues fresh member grants.
    op.execute("""
        ALTER TABLE aw_organizations ADD COLUMN owner_installation_id text REFERENCES aw_installations;
        ALTER TABLE aw_bindings ADD COLUMN org_member boolean NOT NULL DEFAULT false;
    """)


def downgrade():
    raise RuntimeError("Restore an isolated backup to retain the installation and membership fences")
