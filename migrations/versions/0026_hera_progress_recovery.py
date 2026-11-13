"""Discard a damaged derived counter if its delta cannot be applied to a task."""
from alembic import op

revision = "0026_hera_progress_recovery"
down_revision = "0025_hera_progress"
branch_labels = None
depends_on = None


# The original trigger updates both sides when a task moves between branches.
# A failed counter delta must not abort the underlying authoritative task write.
_DELTA = """
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
"""
_DISCARD_DAMAGED = """
    EXCEPTION WHEN data_exception OR not_null_violation THEN
      IF TG_OP IN ('UPDATE','DELETE') THEN
        DELETE FROM nf_hera_progress WHERE work_id=OLD.work_id AND branch_id=OLD.branch_id;
      END IF;
      IF TG_OP IN ('UPDATE','INSERT') THEN
        DELETE FROM nf_hera_progress WHERE work_id=NEW.work_id AND branch_id=NEW.branch_id;
      END IF;
      RETURN NULL;
"""


def install_delta(on_damaged):
    op.execute("CREATE OR REPLACE FUNCTION nf_hera_count_transition() RETURNS trigger "
               "LANGUAGE plpgsql AS $$ BEGIN " + _DELTA + on_damaged + " END $$")


def upgrade():
    install_delta(_DISCARD_DAMAGED)


def downgrade():
    install_delta("")
