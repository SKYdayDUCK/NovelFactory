"""Redis transport state; the existing business Outbox and Inbox retain their semantics."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_bus_publications (
            event_id uuid PRIMARY KEY REFERENCES nf_outbox(outbox_id),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            stream_key text, redis_id text,
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            retry_at timestamptz NOT NULL DEFAULT now(), last_error text,
            lease_token uuid, lease_until timestamptz,
            checked_at timestamptz NOT NULL DEFAULT '-infinity',
            CHECK ((lease_token IS NULL) = (lease_until IS NULL))
        );
        CREATE INDEX nf_bus_publish_queue ON nf_bus_publications(retry_at,event_id) WHERE redis_id IS NULL;
        CREATE INDEX nf_bus_repair_queue ON nf_bus_publications(checked_at,event_id) WHERE redis_id IS NOT NULL;
        CREATE TABLE nf_bus_subscriptions (
            event_id uuid NOT NULL REFERENCES nf_bus_publications(event_id),
            consumer text NOT NULL CHECK (consumer IN ('model','world','compute','recovery','log','statistics')),
            status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','DONE','DEAD')),
            attempts integer NOT NULL DEFAULT 0 CHECK(attempts >= 0), last_error text,
            redis_id text, completed_at timestamptz,
            PRIMARY KEY(event_id,consumer), CHECK((status='DONE')=(completed_at IS NOT NULL))
        );
        CREATE INDEX nf_bus_subscription_progress ON nf_bus_subscriptions(consumer,status,completed_at);
        CREATE TABLE nf_bus_quarantine (
            stream_key text NOT NULL, consumer text NOT NULL, redis_id text NOT NULL,
            error_code text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(stream_key,consumer,redis_id)
        );
        CREATE FUNCTION nf_bus_capture() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN INSERT INTO nf_bus_publications(event_id) VALUES(NEW.outbox_id); RETURN NEW; END $$;
        CREATE TRIGGER nf_bus_outbox_insert AFTER INSERT ON nf_outbox FOR EACH ROW EXECUTE FUNCTION nf_bus_capture()
    """)
    # Old P5 rows are backfilled by the relay in bounded batches.
    # A separate transaction after deployment can backfill while production runs.


def downgrade():
    op.execute("""
        LOCK TABLE nf_outbox,nf_bus_publications,nf_bus_subscriptions,nf_bus_quarantine IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS (SELECT FROM nf_bus_publications WHERE attempts>0 OR redis_id IS NOT NULL)
                    OR EXISTS (SELECT FROM nf_bus_subscriptions)
                    OR EXISTS (SELECT FROM nf_bus_quarantine)
                    OR EXISTS (SELECT FROM nf_inbox WHERE consumer LIKE 'p55:%')
            THEN RAISE EXCEPTION 'Preserve P5.5 transport evidence before rollback'; END IF; END $$;
        DROP TRIGGER nf_bus_outbox_insert ON nf_outbox;
        DROP FUNCTION nf_bus_capture();
        DROP TABLE nf_bus_quarantine,nf_bus_subscriptions,nf_bus_publications
    """)
