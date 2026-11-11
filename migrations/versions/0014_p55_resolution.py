"""Crash-safe pending detection and immutable resolution receipts."""
from alembic import op

revision = "0014_p55_resolution"
down_revision = "0013_p55_semantic"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_semantic_scans (
            event_id uuid PRIMARY KEY REFERENCES nf_outbox(outbox_id),
            work_id uuid NOT NULL, branch_id uuid NOT NULL, world_revision_id uuid NOT NULL,
            FOREIGN KEY(work_id,branch_id,world_revision_id)
                REFERENCES nf_revisions(work_id,branch_id,world_revision_id)
        );
        CREATE INDEX nf_semantic_scans_branch ON nf_semantic_scans(work_id,branch_id,event_id);
        CREATE FUNCTION nf_schedule_semantic_scan() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO nf_semantic_scans(event_id,work_id,branch_id,world_revision_id)
            SELECT NEW.outbox_id,r.work_id,r.branch_id,r.world_revision_id FROM nf_revisions r
            WHERE r.world_revision_id::text=NEW.aggregate_id
              AND NEW.payload->'world_ref'->>'world_revision_id'=NEW.aggregate_id
              AND r.work_id::text=NEW.payload->'world_ref'->>'work_id'
              AND r.branch_id::text=NEW.payload->'world_ref'->>'branch_id';
            RETURN NEW;
        END $$;
        CREATE TRIGGER nf_semantic_scan_enqueue AFTER INSERT ON nf_outbox FOR EACH ROW
            WHEN (NEW.topic='world.accepted' AND NEW.aggregate_type='revision')
            EXECUTE FUNCTION nf_schedule_semantic_scan();

        CREATE TABLE nf_conflict_resolutions (
            case_id uuid PRIMARY KEY REFERENCES nf_conflict_cases(case_id),
            receipt_id uuid UNIQUE NOT NULL, task_id uuid NOT NULL REFERENCES nf_tasks(task_id),
            generation integer NOT NULL CHECK (generation>=0),
            strategy text NOT NULL, outcome text NOT NULL, details jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX nf_resolution_task ON nf_conflict_resolutions(task_id,generation);
        CREATE TRIGGER nf_resolution_immutable BEFORE UPDATE OR DELETE ON nf_conflict_resolutions
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_outbox,nf_semantic_scans,nf_conflict_resolutions IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_conflict_resolutions) OR EXISTS(SELECT FROM nf_semantic_scans)
            THEN RAISE EXCEPTION 'Archive conflict receipts and drain semantic scans before downgrade';
        END IF; END $$;
        DROP TRIGGER nf_semantic_scan_enqueue ON nf_outbox;
        DROP FUNCTION nf_schedule_semantic_scan();
        DROP TABLE nf_conflict_resolutions,nf_semantic_scans;
    """)
