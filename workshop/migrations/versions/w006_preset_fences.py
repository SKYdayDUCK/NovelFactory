"""Maintain preset ownership, admission limits and message origin fencing across upgrades."""

from alembic import op

revision = "w006"
down_revision = "w005"


def upgrade():
    op.execute("""
      ALTER TABLE aw_organizations
        ADD COLUMN max_active_employees integer CHECK(max_active_employees>0),
        ADD COLUMN max_active_delegations integer CHECK(max_active_delegations>0),
        ADD COLUMN team_policy jsonb;
      CREATE TABLE aw_preset_members (
        org_id text NOT NULL, employee_id text NOT NULL, managed_revision bigint NOT NULL,
        parked boolean NOT NULL DEFAULT false, PRIMARY KEY(org_id,employee_id),
        FOREIGN KEY (org_id,employee_id,managed_revision) REFERENCES aw_employment_versions
      );
      INSERT INTO aw_preset_members
        SELECT org_id,employee_id,employment_revision,false FROM aw_employment_versions WHERE org_revision=1;
      CREATE TABLE aw_message_origins (
        org_id text NOT NULL, message_id uuid NOT NULL, token_digest char(64) NOT NULL REFERENCES aw_bindings,
        grant_epoch bigint NOT NULL, PRIMARY KEY(org_id,message_id), FOREIGN KEY(org_id,message_id) REFERENCES aw_messages
      );
    """)


def downgrade():
    raise RuntimeError("Restore an approved backup to preserve preset and sender provenance")
