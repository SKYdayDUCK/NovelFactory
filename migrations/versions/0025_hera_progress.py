"""Opt-in, rebuildable progress counters. Source tasks remain authoritative."""
from alembic import op

revision = "0025_hera_progress"
down_revision = "0024_hera_resources"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_hera_progress (
          work_id uuid NOT NULL, branch_id uuid NOT NULL,
          counts jsonb NOT NULL, watermark bigint NOT NULL DEFAULT 0,
          PRIMARY KEY(work_id,branch_id),
          FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        CREATE FUNCTION nf_hera_count_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='UPDATE' THEN
            IF (NEW.work_id,NEW.branch_id,NEW.state)=(OLD.work_id,OLD.branch_id,OLD.state) THEN
              RETURN NULL;
            END IF;
          END IF;
          IF TG_OP IN ('UPDATE','DELETE') THEN
            UPDATE nf_hera_progress SET counts=jsonb_set(counts,ARRAY[OLD.state::text],
                to_jsonb((counts->>OLD.state)::bigint-1)), watermark=watermark+1
            WHERE work_id=OLD.work_id AND branch_id=OLD.branch_id;
          END IF;
          IF TG_OP IN ('UPDATE','INSERT') THEN
            UPDATE nf_hera_progress SET counts=jsonb_set(counts,ARRAY[NEW.state::text],
                to_jsonb(COALESCE((counts->>NEW.state)::bigint,0)+1)), watermark=watermark+1
            WHERE work_id=NEW.work_id AND branch_id=NEW.branch_id;
          END IF;
          RETURN NULL;
        END $$;
        CREATE TRIGGER nf_hera_progress_delta AFTER INSERT OR UPDATE OF state,work_id,branch_id
          OR DELETE ON nf_tasks FOR EACH ROW EXECUTE FUNCTION nf_hera_count_transition();
    """)


def downgrade():
    op.execute("""
        DROP TRIGGER nf_hera_progress_delta ON nf_tasks;
        DROP FUNCTION nf_hera_count_transition();
        DROP TABLE nf_hera_progress;
    """)
