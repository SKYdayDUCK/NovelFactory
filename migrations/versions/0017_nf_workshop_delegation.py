"""Add installation bindings, one-use runtime slots and durable delegation receipts."""

from alembic import op

revision = "0017_nf_workshop_delegation"
down_revision = "0016_p55_observed_scopes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_delegation_installations (
            installation_id uuid PRIMARY KEY, nf_instance_id uuid NOT NULL, org_id uuid NOT NULL,
            work_id uuid NOT NULL, branch_id uuid NOT NULL,
            secret_hash varchar(64) NOT NULL UNIQUE, policy_version varchar NOT NULL,
            epoch bigint NOT NULL DEFAULT 1, active boolean NOT NULL DEFAULT true,
            outbound_fenced boolean NOT NULL DEFAULT false, expires_at timestamptz NOT NULL,
            FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        CREATE TABLE nf_delegation_slots (
            installation_id uuid NOT NULL REFERENCES nf_delegation_installations,
            scope_ref varchar(128) NOT NULL, task_template_id varchar(128) NOT NULL,
            grant_id uuid NOT NULL UNIQUE REFERENCES nf_grants, grant_epoch bigint NOT NULL,
            principal_id uuid NOT NULL, actor_binding_ref uuid, spec jsonb NOT NULL, job_config jsonb NOT NULL,
            PRIMARY KEY(installation_id,scope_ref)
        );
        CREATE TABLE nf_delegation_receipts (
            installation_id uuid NOT NULL, operation_id uuid NOT NULL, scope_ref varchar(128) NOT NULL,
            budget_reservation_ref uuid NOT NULL, task_id uuid NOT NULL UNIQUE REFERENCES nf_tasks,
            receipt_ref uuid NOT NULL UNIQUE, request_hash varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(installation_id,operation_id), UNIQUE(installation_id,scope_ref),
            UNIQUE(installation_id,budget_reservation_ref),
            FOREIGN KEY(installation_id,scope_ref) REFERENCES nf_delegation_slots(installation_id,scope_ref)
        );
        CREATE TRIGGER immutable_delegation_slot BEFORE UPDATE OR DELETE ON nf_delegation_slots
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        CREATE TRIGGER immutable_delegation_receipt BEFORE UPDATE OR DELETE ON nf_delegation_receipts
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_delegation_installations,nf_delegation_slots,nf_delegation_receipts IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_delegation_installations) THEN
            RAISE EXCEPTION 'Retain delegation bindings and receipts before downgrade'; END IF; END $$;
        DROP TABLE nf_delegation_receipts,nf_delegation_slots,nf_delegation_installations;
    """)
