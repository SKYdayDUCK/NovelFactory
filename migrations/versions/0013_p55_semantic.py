"""Append-only read receipts and an index over accepted world changes.

Existing immutable task inputs, contexts, events and selections remain authority.
No rewrite of a world revision is required. Old unfenced inputs fail closed
across a changed HEAD. Retain this additive schema during a code rollback.
"""
from alembic import op

revision = "0013_p55_semantic"
down_revision = "0012_p55_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE nf_audit_events ADD COLUMN conflict_details jsonb;
        CREATE UNIQUE INDEX nf_message_source_revision ON nf_receipts
            (work_id,(payload->'source_ref'->>'artifact_revision_id')) WHERE command_type='MESSAGE_SOURCE_VERSION';
        CREATE TABLE nf_semantic_reads (
            observation_id uuid PRIMARY KEY, task_id uuid NOT NULL, generation integer NOT NULL,
            work_id uuid NOT NULL, branch_id uuid NOT NULL, world_revision_id uuid NOT NULL,
            branch_seq bigint NOT NULL, keys text[] NOT NULL, details jsonb NOT NULL,
            FOREIGN KEY(task_id,generation) REFERENCES nf_task_inputs(task_id,generation),
            FOREIGN KEY(work_id,branch_id,world_revision_id)
                REFERENCES nf_revisions(work_id,branch_id,world_revision_id)
        );
        CREATE INDEX nf_sem_read_task ON nf_semantic_reads(task_id,generation);
        CREATE INDEX nf_sem_read_keys ON nf_semantic_reads USING gin(keys);
        CREATE INDEX nf_sem_read_branch ON nf_semantic_reads(branch_id,branch_seq);
        CREATE TABLE nf_semantic_changes (
            world_revision_id uuid PRIMARY KEY, work_id uuid NOT NULL, branch_id uuid NOT NULL,
            branch_seq bigint NOT NULL, source_task_id uuid REFERENCES nf_tasks(task_id),
            keys text[] NOT NULL, details jsonb NOT NULL,
            FOREIGN KEY(work_id,branch_id,world_revision_id)
                REFERENCES nf_revisions(work_id,branch_id,world_revision_id)
        );
        CREATE INDEX nf_sem_change_keys ON nf_semantic_changes USING gin(keys);
        CREATE INDEX nf_sem_change_branch ON nf_semantic_changes(branch_id,branch_seq);
        CREATE TABLE nf_conflict_cases (
            case_id uuid PRIMARY KEY, work_id uuid NOT NULL, branch_id uuid NOT NULL,
            task_id uuid NOT NULL, generation integer NOT NULL,
            source_task_id uuid REFERENCES nf_tasks(task_id),
            world_revision_id uuid NOT NULL REFERENCES nf_semantic_changes(world_revision_id),
            conflict_type text NOT NULL, strategy text NOT NULL, details jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY(task_id,generation) REFERENCES nf_task_inputs(task_id,generation),
            FOREIGN KEY(work_id,branch_id,world_revision_id)
                REFERENCES nf_revisions(work_id,branch_id,world_revision_id),
            UNIQUE(task_id,generation,world_revision_id)
        );
        CREATE VIEW nf_semantic_manifests AS
            SELECT t.task_id, i.generation, t.work_id, t.branch_id, t.role_id, g.actor_id,
                   coalesce(trace.trace_id,t.task_id) AS workflow_id,
                   i.spec->'world_ref' AS base_world_ref, i.spec->'story_time' AS task_story_time,
                   i.spec->'input_artifacts' AS input_artifacts,
                   r.observation_id, r.world_revision_id, r.keys, r.details,
                   c.context_bundle_id, c.dependency_fingerprint
            FROM nf_task_inputs i JOIN nf_tasks t USING(task_id) JOIN nf_grants g ON g.grant_id=t.grant_id
            LEFT JOIN nf_trace_tasks trace USING(task_id)
            LEFT JOIN nf_contexts c ON (c.task_id,c.generation)=(i.task_id,i.generation)
            LEFT JOIN nf_semantic_reads r ON (r.task_id,r.generation)=(i.task_id,i.generation);
        CREATE TRIGGER immutable_semantic_read BEFORE UPDATE OR DELETE ON nf_semantic_reads
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        CREATE TRIGGER immutable_semantic_change BEFORE UPDATE OR DELETE ON nf_semantic_changes
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        CREATE TRIGGER immutable_conflict_case BEFORE UPDATE OR DELETE ON nf_conflict_cases
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_semantic_reads, nf_semantic_changes, nf_conflict_cases, nf_audit_events
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS(SELECT FROM nf_semantic_reads) OR EXISTS(SELECT FROM nf_semantic_changes)
                OR EXISTS(SELECT FROM nf_conflict_cases)
                OR EXISTS(SELECT FROM nf_audit_events WHERE conflict_details IS NOT NULL)
                OR EXISTS(SELECT FROM nf_receipts WHERE command_type='MESSAGE_SOURCE_VERSION')
            THEN RAISE EXCEPTION 'Archive semantic receipts before downgrade'; END IF;
        END $$;
        DROP VIEW nf_semantic_manifests;
        DROP INDEX nf_message_source_revision;
        DROP TABLE nf_conflict_cases, nf_semantic_changes, nf_semantic_reads;
        ALTER TABLE nf_audit_events DROP COLUMN conflict_details;
    """)
