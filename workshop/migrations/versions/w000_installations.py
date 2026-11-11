"""Provision server-owned installation and scoped, expiring connection identities."""
from alembic import op

revision = "w000"
down_revision = None


def upgrade():
    op.execute("""
        CREATE TABLE aw_installations (
            installation_id text PRIMARY KEY,
            nf_instance_id text NOT NULL,
            enabled boolean NOT NULL DEFAULT true
        );
        CREATE TABLE aw_bindings (
            token_digest char(64) PRIMARY KEY,
            installation_id text NOT NULL REFERENCES aw_installations,
            org_id text NOT NULL,
            employee_id text NOT NULL,
            employment_revision bigint NOT NULL CHECK (employment_revision > 0),
            grant_epoch bigint NOT NULL CHECK (grant_epoch > 0),
            expires_at timestamptz NOT NULL,
            revoked boolean NOT NULL DEFAULT false
        );
        CREATE INDEX aw_bindings_employee ON aw_bindings (org_id, employee_id);
    """)


def downgrade():
    raise RuntimeError("Restore the paired backup to an isolated instance for rollback")
