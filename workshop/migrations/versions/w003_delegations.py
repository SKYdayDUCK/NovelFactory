"""Workshop-owned delegation intents, budget holds, attempt observations and delivery fences."""

from alembic import op

revision = "w003"
down_revision = "w002"


def upgrade():
    op.execute("""
        CREATE TABLE aw_budget_accounts (
            org_id text PRIMARY KEY REFERENCES aw_organizations,
            limit_amount numeric(20,6) NOT NULL CHECK (limit_amount >= 0),
            currency text NOT NULL CHECK (currency IN ('USD','CNY'))
        );
        CREATE TABLE aw_delegation_slots (
            org_id text NOT NULL REFERENCES aw_organizations,
            slot_id text NOT NULL, installation_id text NOT NULL REFERENCES aw_installations,
            employee_id text NOT NULL, employment_revision bigint NOT NULL,
            config jsonb NOT NULL, enabled boolean NOT NULL DEFAULT true,
            PRIMARY KEY(org_id,slot_id),
            FOREIGN KEY(org_id,employee_id,employment_revision) REFERENCES aw_employment_versions
        );
        CREATE TABLE aw_delegations (
            org_id text NOT NULL, operation_id uuid NOT NULL,
            client_key text NOT NULL, sender_id text NOT NULL, sender_revision bigint NOT NULL,
            binding_digest char(64) NOT NULL REFERENCES aw_bindings,
            slot_id text NOT NULL, installation_id text NOT NULL,
            payload jsonb NOT NULL, request_sha char(64) NOT NULL, state text NOT NULL,
            owner_id text NOT NULL, owner_revision bigint NOT NULL,
            fence bigint NOT NULL DEFAULT 0, next_check timestamptz NOT NULL DEFAULT now(),
            deadline timestamptz NOT NULL, last_error text, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(org_id,operation_id), UNIQUE(installation_id,operation_id),
            UNIQUE(org_id,sender_id,sender_revision,client_key), UNIQUE(org_id,slot_id),
            FOREIGN KEY(org_id,slot_id) REFERENCES aw_delegation_slots
        );
        CREATE INDEX aw_delegations_ready ON aw_delegations(installation_id,next_check,created_at);
        CREATE TABLE aw_budget_holds (
            org_id text NOT NULL, operation_id uuid NOT NULL, amount numeric(20,6) NOT NULL CHECK(amount>0),
            status text NOT NULL CHECK(status IN ('HELD','SETTLED','RELEASED')),
            settled_amount numeric(20,6), PRIMARY KEY(org_id,operation_id),
            FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
        );
        CREATE TABLE aw_remote_receipts (
            org_id text NOT NULL, operation_id uuid NOT NULL, task_id uuid NOT NULL, receipt_ref uuid NOT NULL,
            generation bigint NOT NULL, task_state text NOT NULL, accounting_state text NOT NULL,
            PRIMARY KEY(org_id,operation_id), FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
        );
        CREATE TABLE aw_cost_observations (
            org_id text NOT NULL, operation_id uuid NOT NULL, attempt_id uuid NOT NULL, observation_sha char(64) NOT NULL,
            state text NOT NULL, actual_amount numeric(20,6), actual_tokens bigint, observed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(org_id,operation_id,attempt_id,observation_sha),
            FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
        );
        CREATE TABLE aw_handoffs (
            org_id text NOT NULL, handoff_id text NOT NULL, operation_id uuid NOT NULL, sender text NOT NULL,
            request_sha char(64) NOT NULL, recipient text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(org_id,sender,handoff_id), FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
        );
    """)


def downgrade():
    raise RuntimeError(
        "Restore the paired backup to preserve unsettled holds and original receipts"
    )
