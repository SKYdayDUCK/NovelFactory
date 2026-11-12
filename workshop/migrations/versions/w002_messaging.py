"""Addressed mail, non-transferrable routes, PostgreSQL delivery log."""

from alembic import op

revision = "w002"
down_revision = "w001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE aw_route_grants (
            org_id text NOT NULL, grant_id uuid NOT NULL, issuer text NOT NULL,
            src text NOT NULL, dst text NOT NULL, src_revision bigint NOT NULL, dst_revision bigint NOT NULL,
            issuer_revision bigint NOT NULL, allowed_kind text NOT NULL, scope_ref text NOT NULL,
            expires_at timestamptz NOT NULL, max_uses integer NOT NULL CHECK (max_uses > 0),
            consumed_uses integer NOT NULL DEFAULT 0 CHECK (consumed_uses BETWEEN 0 AND max_uses),
            revocation_epoch bigint NOT NULL DEFAULT 1, revoked boolean NOT NULL DEFAULT false,
            PRIMARY KEY(org_id,grant_id), FOREIGN KEY(org_id,src) REFERENCES aw_employees,
            FOREIGN KEY(org_id,dst) REFERENCES aw_employees, FOREIGN KEY(org_id,issuer) REFERENCES aw_employees
        );
        CREATE TABLE aw_message_bodies (
            org_id text NOT NULL, body_ref uuid NOT NULL, src text NOT NULL, dst text NOT NULL,
            src_revision bigint NOT NULL, dst_revision bigint NOT NULL, scope_ref text NOT NULL,
            content text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 16384),
            PRIMARY KEY(org_id,body_ref)
        );
        CREATE TABLE aw_mailboxes (
            org_id text NOT NULL, recipient text NOT NULL, employment_revision bigint NOT NULL,
            seq bigint NOT NULL CHECK(seq>0), PRIMARY KEY(org_id,recipient,employment_revision)
        );
        CREATE TABLE aw_messages (
            org_id text NOT NULL, message_id uuid NOT NULL, operation_id text NOT NULL,
            sender text NOT NULL, sender_revision bigint NOT NULL, recipient text NOT NULL,
            recipient_revision bigint NOT NULL, graph_revision bigint NOT NULL,
            kind text NOT NULL, scope_ref text NOT NULL, body_ref uuid, route_grant_id uuid,
            grant_epoch bigint, causation_id uuid, root_id uuid NOT NULL, hops integer NOT NULL,
            incident_code text, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(org_id,message_id), FOREIGN KEY(org_id,graph_revision) REFERENCES aw_org_revisions,
            FOREIGN KEY(org_id,causation_id) REFERENCES aw_messages,
            FOREIGN KEY(org_id,body_ref) REFERENCES aw_message_bodies,
            FOREIGN KEY(org_id,route_grant_id) REFERENCES aw_route_grants,
            UNIQUE(org_id,sender,sender_revision,operation_id)
        );
        CREATE INDEX aw_incidents ON aw_messages(org_id,sender,created_at) WHERE kind='INCIDENT';
        CREATE TABLE aw_deliveries (
            org_id text NOT NULL, message_id uuid NOT NULL, recipient text NOT NULL,
            recipient_revision bigint NOT NULL, seq bigint NOT NULL, acknowledged_at timestamptz,
            PRIMARY KEY(org_id,message_id), FOREIGN KEY(org_id,message_id) REFERENCES aw_messages,
            UNIQUE(org_id,recipient,recipient_revision,seq)
        );
        CREATE TABLE aw_outbox (
            event_id uuid PRIMARY KEY, org_id text NOT NULL, message_id uuid NOT NULL,
            last_notified timestamptz, UNIQUE(org_id,message_id),
            FOREIGN KEY(org_id,message_id) REFERENCES aw_messages
        );
        CREATE TABLE aw_inbox (
            subscriber text NOT NULL, event_id uuid NOT NULL REFERENCES aw_outbox,
            received_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(subscriber,event_id)
        );
        CREATE TABLE aw_message_operations (
            org_id text NOT NULL, employee_id text NOT NULL, employment_revision bigint NOT NULL,
            operation_id text NOT NULL, request_sha text NOT NULL, receipt jsonb NOT NULL,
            PRIMARY KEY(org_id,employee_id,employment_revision,operation_id)
        );
    """)


def downgrade():
    raise RuntimeError("Restore the backup to preserve all message receipts and route history")
