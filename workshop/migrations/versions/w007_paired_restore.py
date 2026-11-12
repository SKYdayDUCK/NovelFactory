"""Keep all outbound submissions fenced across restarts after a partner restore."""

from alembic import op

revision = "w007"
down_revision = "w006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS aw_recovery_fences (
        installation_id text PRIMARY KEY REFERENCES aw_installations(installation_id),
        epoch bigint NOT NULL CHECK(epoch>0), frozen boolean NOT NULL,
        reason text NOT NULL, changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS aw_recovery_proofs(
        installation_id text NOT NULL REFERENCES aw_installations,
        org_id text NOT NULL, operation_id uuid NOT NULL,
        epoch bigint NOT NULL CHECK(epoch>0), ledger jsonb,
        PRIMARY KEY(installation_id,org_id,operation_id),
        FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
    )""")


def downgrade():
    op.execute("""LOCK TABLE aw_recovery_fences IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM aw_recovery_fences) THEN
            RAISE EXCEPTION 'Archive recovery decisions before migrating back'; END IF; END $$;
        DROP TABLE aw_recovery_proofs, aw_recovery_fences""")
