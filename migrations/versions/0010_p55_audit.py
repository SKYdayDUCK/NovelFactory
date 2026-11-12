"""Append-only, scoped audit references. No historical payload is rewritten."""
from alembic import op

revision = "0010_p55_audit"
down_revision = "0009a_p55_capacity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_trace_tasks (
            task_id uuid PRIMARY KEY REFERENCES nf_tasks(task_id),
            trace_id uuid NOT NULL, volume_id uuid, parent_task_id uuid REFERENCES nf_tasks(task_id)
        );
        CREATE INDEX nf_trace_tasks_scene ON nf_trace_tasks(trace_id);
        CREATE TABLE nf_audit_events (
            event_id uuid PRIMARY KEY,
            work_id uuid NOT NULL REFERENCES nf_works(work_id),
            branch_id uuid, event_type text NOT NULL,
            trace_id uuid NOT NULL, span_id uuid NOT NULL, parent_span_id uuid,
            causation_id uuid, task_id uuid REFERENCES nf_tasks(task_id),
            generation integer CHECK(generation >= 0), actor_id uuid,
            role_id text, principal_id uuid, world_revision_id uuid, attempt_id uuid,
            volume_id uuid, experiment_id uuid, model text,
            request_id text, outcome text, error_code text, tool_name text,
            duration_ms numeric CHECK(duration_ms >= 0),
            text_characters integer CHECK(text_characters >= 0),
            evidence_id uuid, event_time timestamptz NOT NULL,
            recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY(work_id,branch_id) REFERENCES nf_branches(work_id,branch_id)
        );
        CREATE INDEX nf_audit_scope_cut ON nf_audit_events(work_id,branch_id,event_time,event_id);
        CREATE INDEX nf_audit_trace ON nf_audit_events(work_id,trace_id,event_time);
        CREATE INDEX nf_audit_task ON nf_audit_events(task_id,generation);
        CREATE FUNCTION nf_reject_audit_edit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit history is immutable'; END $$;
        CREATE TRIGGER nf_audit_immutable BEFORE UPDATE OR DELETE ON nf_audit_events
        FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_audit_events, nf_trace_tasks IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM nf_audit_events) OR EXISTS(SELECT 1 FROM nf_trace_tasks) THEN
            RAISE EXCEPTION 'Preserve and restore audit history before removing this migration';
          END IF;
        END $$;
        DROP TABLE nf_audit_events, nf_trace_tasks;
        DROP FUNCTION nf_reject_audit_edit()
    """)
