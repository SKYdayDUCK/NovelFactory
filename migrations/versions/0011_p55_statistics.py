"""Versioned, rebuildable per-event metric contributions."""

from alembic import op

revision = "0011_p55_statistics"
down_revision = "0010_p55_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_metric_cuts (
          cut_id text PRIMARY KEY, work_id uuid NOT NULL REFERENCES nf_works(work_id), branch_id uuid,
          last_used_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          event_ids uuid[] NOT NULL,
          FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        CREATE INDEX nf_metric_cut_gc ON nf_metric_cuts(work_id,branch_id,last_used_at DESC);
        CREATE TABLE nf_metric_experiments(
          experiment_id uuid PRIMARY KEY, work_id uuid NOT NULL, branch_id uuid NOT NULL,
          starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL CHECK(ends_at > starts_at),
          FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        CREATE TABLE nf_metric_contributions(
          metric_version smallint NOT NULL CHECK(metric_version > 0),
          event_id uuid NOT NULL REFERENCES nf_audit_events(event_id),
          metrics jsonb NOT NULL CHECK(jsonb_typeof(metrics)='object'),
          computed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(metric_version,event_id)
        )
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_metric_contributions IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM nf_metric_contributions) OR EXISTS(SELECT 1 FROM nf_metric_experiments) THEN
          RAISE EXCEPTION 'Archive or explicitly clear derived projections first';
        END IF; END $$;
        DROP TABLE nf_metric_contributions, nf_metric_cuts, nf_metric_experiments
    """)
