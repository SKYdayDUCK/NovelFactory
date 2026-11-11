"""Extend the published S9 checkpoint without rewriting its original receipts."""
from alembic import op

revision = "0015_p55_revalidation"
down_revision = "0014_p55_resolution"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        -- A historic registration time is unknown. Include those receipts in any
        -- pre-existing material lineage, and mark them for conservative validation.
        ALTER TABLE nf_semantic_reads ADD COLUMN created_at timestamptz;
        ALTER TABLE nf_semantic_reads ALTER COLUMN created_at SET DEFAULT clock_timestamp();
        CREATE INDEX nf_sem_legacy_read ON nf_semantic_reads(branch_id,branch_seq) WHERE created_at IS NULL;
        CREATE TABLE nf_semantic_upgrade_cuts (
            work_id uuid NOT NULL, branch_id uuid PRIMARY KEY, branch_seq bigint NOT NULL,
            FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        INSERT INTO nf_semantic_upgrade_cuts SELECT work_id,branch_id,max(branch_seq)
            FROM nf_semantic_changes GROUP BY work_id,branch_id;
        CREATE TABLE nf_semantic_revalidations (
            observation_id uuid NOT NULL REFERENCES nf_semantic_reads(observation_id),
            work_id uuid NOT NULL, branch_id uuid NOT NULL, world_revision_id uuid NOT NULL,
            branch_seq bigint NOT NULL, proof_hash text NOT NULL,
            PRIMARY KEY(observation_id,world_revision_id),
            FOREIGN KEY(work_id,branch_id,world_revision_id)
                REFERENCES nf_revisions(work_id,branch_id,world_revision_id)
        );
        CREATE INDEX nf_sem_proof_watermark ON nf_semantic_revalidations(observation_id,branch_seq);
        CREATE TRIGGER immutable_semantic_revalidation BEFORE UPDATE OR DELETE ON nf_semantic_revalidations
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        ALTER TABLE nf_conflict_cases
            DROP CONSTRAINT nf_conflict_cases_world_revision_id_fkey,
            DROP CONSTRAINT nf_conflict_cases_task_id_generation_world_revision_id_key,
            ADD CONSTRAINT nf_sem_case_kind_unique UNIQUE(task_id,generation,world_revision_id,conflict_type);
        CREATE INDEX nf_semantic_producer ON nf_audit_events(evidence_id) WHERE task_id IS NOT NULL;
        CREATE OR REPLACE FUNCTION nf_schedule_semantic_scan() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO nf_semantic_scans(event_id,work_id,branch_id,world_revision_id)
            SELECT NEW.outbox_id,r.work_id,r.branch_id,r.world_revision_id FROM nf_revisions r
            WHERE r.world_revision_id=CASE WHEN pg_input_is_valid(NEW.aggregate_id,'uuid')
                        THEN NEW.aggregate_id::uuid END
              AND NEW.payload->'world_ref'->>'world_revision_id'=NEW.aggregate_id
              AND r.work_id::text=NEW.payload->'world_ref'->>'work_id'
              AND r.branch_id::text=NEW.payload->'world_ref'->>'branch_id';
            RETURN NEW;
        END $$;
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_semantic_revalidations,nf_conflict_cases IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_semantic_revalidations)
            THEN RAISE EXCEPTION 'Archive revalidation proofs before downgrade'; END IF; END $$;
        -- PostgreSQL validates both old constraints. If new-style branch cases
        -- cannot be represented at 0014 the whole downgrade rolls back.
        ALTER TABLE nf_conflict_cases
            DROP CONSTRAINT nf_sem_case_kind_unique,
            ADD UNIQUE(task_id,generation,world_revision_id),
            ADD FOREIGN KEY(world_revision_id) REFERENCES nf_semantic_changes(world_revision_id);
        DROP INDEX nf_semantic_producer, nf_sem_legacy_read;
        DROP TABLE nf_semantic_revalidations, nf_semantic_upgrade_cuts;
        ALTER TABLE nf_semantic_reads DROP COLUMN created_at;
        CREATE OR REPLACE FUNCTION nf_schedule_semantic_scan() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO nf_semantic_scans(event_id,work_id,branch_id,world_revision_id)
            SELECT NEW.outbox_id,r.work_id,r.branch_id,r.world_revision_id FROM nf_revisions r
            WHERE r.world_revision_id::text=NEW.aggregate_id
              AND NEW.payload->'world_ref'->>'world_revision_id'=NEW.aggregate_id
              AND r.work_id::text=NEW.payload->'world_ref'->>'work_id'
              AND r.branch_id::text=NEW.payload->'world_ref'->>'branch_id';
            RETURN NEW;
        END $$;
    """)
