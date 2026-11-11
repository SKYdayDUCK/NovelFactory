"""Database-wide supervisor admission leases and commit-time wake hints."""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_dispatch_limits (
            scope text NOT NULL CHECK (scope IN ('GLOBAL','WORK','ROLE')),
            subject text NOT NULL CHECK (subject <> ''),
            slots integer NOT NULL CHECK (slots >= 0),
            PRIMARY KEY (scope, subject),
            CHECK (scope <> 'GLOBAL' OR subject = '*')
        )
    """)
    op.execute("""
        INSERT INTO nf_dispatch_limits VALUES ('GLOBAL','*',2), ('WORK','*',2), ('ROLE','*',2)
    """)
    op.execute("""
        CREATE TABLE nf_dispatch_waiters (
            branch_id uuid PRIMARY KEY REFERENCES nf_branches(branch_id),
            requested_at timestamptz NOT NULL, valid_until timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE nf_dispatch_slots (
            attempt_id uuid PRIMARY KEY REFERENCES nf_task_executions(attempt_id),
            task_id uuid NOT NULL REFERENCES nf_supervisor_jobs(task_id),
            expires_at timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE FUNCTION nf_signal_supervisor() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('nf_supervisor_wake', '');
            RETURN NULL;
        END $$
    """)
    # Row triggers ignore ON CONFLICT DO NOTHING replays. PostgreSQL coalesces
    # identical notifications within a transaction and delivers only on commit.
    op.execute("""
        CREATE TRIGGER nf_outbox_wake AFTER INSERT ON nf_outbox FOR EACH ROW
        WHEN (NEW.topic IN ('task.changed','world.accepted','authorization.revoked','continuity.published'))
        EXECUTE FUNCTION nf_signal_supervisor()
    """)
    op.execute("""CREATE TRIGGER nf_enroll_wake AFTER INSERT ON nf_supervisor_jobs
                  FOR EACH ROW EXECUTE FUNCTION nf_signal_supervisor()""")
    op.execute("""CREATE TRIGGER nf_release_wake AFTER DELETE ON nf_dispatch_slots
                  FOR EACH ROW EXECUTE FUNCTION nf_signal_supervisor()""")
    op.execute("""CREATE TRIGGER nf_limit_wake AFTER INSERT OR UPDATE OR DELETE ON nf_dispatch_limits
                  FOR EACH STATEMENT EXECUTE FUNCTION nf_signal_supervisor()""")


def downgrade():
    op.execute("LOCK TABLE nf_dispatch_slots, nf_dispatch_limits IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM nf_dispatch_slots WHERE expires_at > clock_timestamp()) OR
           EXISTS (SELECT 1 FROM nf_dispatch_limits WHERE subject <> '*' OR slots <> 2) OR
           (SELECT count(*) FROM nf_dispatch_limits) <> 3
        THEN RAISE EXCEPTION 'Drain supervisor slots and preserve/reset custom limits before rollback';
        END IF;
    END $$""")
    op.execute("DROP TRIGGER nf_outbox_wake ON nf_outbox")
    op.execute("DROP TRIGGER nf_enroll_wake ON nf_supervisor_jobs")
    op.drop_table("nf_dispatch_slots")
    op.drop_table("nf_dispatch_waiters")
    op.drop_table("nf_dispatch_limits")
    op.execute("DROP FUNCTION nf_signal_supervisor()")
