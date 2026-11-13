"""Opt-in, installation-scoped one-shot project wake on a settled delegation."""
from alembic import op

revision = "w013"
down_revision = "w012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE aw_resume_requests ADD COLUMN after_delegation_id uuid,
          ADD FOREIGN KEY (org_id,after_delegation_id) REFERENCES aw_delegations(org_id,operation_id);
        CREATE INDEX aw_resume_on_delegation ON aw_resume_requests(org_id,installation_id,after_delegation_id)
          WHERE state='WAITING_DELEGATION';
        CREATE FUNCTION aw_wake_settled_delegation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE aw_resume_requests SET state='PENDING', eligible_at=clock_timestamp()
            WHERE org_id=NEW.org_id AND installation_id=NEW.installation_id
              AND after_delegation_id=NEW.operation_id AND state='WAITING_DELEGATION';
          RETURN NULL;
        END $$;
        CREATE TRIGGER aw_delegation_settled AFTER UPDATE OF state ON aw_delegations
          FOR EACH ROW WHEN (NEW.state='SETTLED' AND OLD.state IS DISTINCT FROM NEW.state)
          EXECUTE FUNCTION aw_wake_settled_delegation();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE aw_resume_requests IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS (SELECT FROM aw_resume_requests WHERE after_delegation_id IS NOT NULL)
          THEN RAISE EXCEPTION 'Retain subscriptions and wake evidence in w013'; END IF; END $$;
        DROP TRIGGER aw_delegation_settled ON aw_delegations;
        DROP FUNCTION aw_wake_settled_delegation();
        ALTER TABLE aw_resume_requests DROP COLUMN after_delegation_id;
    """)
