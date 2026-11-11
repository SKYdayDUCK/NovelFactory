"""Transient compute reservations and bounded scaling decision history."""
from alembic import op

revision = "0009a_p55_capacity"
down_revision = "0009_p55_processors"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE nf_index_leases (
        event_id uuid PRIMARY KEY REFERENCES nf_outbox(outbox_id), token uuid NOT NULL,
        work_id uuid NOT NULL, branch_id uuid NOT NULL, expires_at timestamptz NOT NULL,
        FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id))""")
    op.execute("""CREATE TRIGGER nf_index_release AFTER DELETE ON nf_index_leases FOR EACH ROW
        EXECUTE FUNCTION nf_signal_supervisor()""")
    op.execute("""CREATE TABLE nf_scale_decisions (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, processor text NOT NULL,
        instance uuid NOT NULL, work_id uuid REFERENCES nf_works(work_id), created_at timestamptz NOT NULL DEFAULT now(),
        previous integer NOT NULL CHECK(previous>=1), target integer NOT NULL CHECK(target>=1),
        reason text NOT NULL, sample jsonb NOT NULL CHECK(jsonb_typeof(sample)='object'))""")
    op.execute("CREATE INDEX nf_scale_instances ON nf_scale_decisions(instance,id DESC)")


def downgrade():
    op.execute("LOCK TABLE nf_index_leases,nf_scale_decisions IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM nf_index_leases WHERE expires_at>now())
        OR EXISTS (SELECT FROM nf_scale_decisions)
        THEN RAISE EXCEPTION 'Drain index preparations and archive scaling records before downgrade';
        END IF; END $$""")
    op.drop_table("nf_scale_decisions")
    op.drop_table("nf_index_leases")
