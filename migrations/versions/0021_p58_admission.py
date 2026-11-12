"""Shared runtime quotas and expiring send permits. No changes to historical attempts."""
from alembic import op

revision = "0021_p58_admission"
down_revision = "0021_p58_routing"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_runtime_limits (
          scope text CHECK (scope IN ('provider','account','organization','work','role')),
          subject text CHECK (length(subject) BETWEEN 1 AND 256),
          slots integer NOT NULL CHECK (slots >= 0),
          rpm integer CHECK (rpm >= 0), tpm bigint CHECK (tpm >= 0),
          PRIMARY KEY (scope,subject));
        CREATE TABLE nf_runtime_permits (
          attempt_id uuid PRIMARY KEY REFERENCES nf_call_attempts,
          task_id uuid NOT NULL REFERENCES nf_tasks,
          scope jsonb NOT NULL, tokens bigint NOT NULL CHECK(tokens>=0),
          started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          expires_at timestamptz NOT NULL, released boolean NOT NULL DEFAULT false);
        CREATE INDEX nf_runtime_permits_window ON nf_runtime_permits(started_at);
        CREATE INDEX nf_runtime_permits_live ON nf_runtime_permits(expires_at) WHERE NOT released;
        CREATE FUNCTION nf_runtime_scope(raw_scope jsonb, work uuid, branch uuid)
        RETURNS jsonb LANGUAGE sql STABLE AS $$
          SELECT CASE WHEN raw_scope IS NULL OR raw_scope ? 'organization' THEN raw_scope
            ELSE raw_scope || coalesce((
              SELECT jsonb_build_object('organization',(a.payload->>'installation_id') || '/' || (a.payload->>'org_id'))
              FROM nf_selections s JOIN nf_artifacts a ON a.work_id=s.work_id AND
                a.artifact_revision_id::text=s.artifact_ref->>'artifact_revision_id'
              WHERE s.work_id=work AND s.selection_kind='RUNTIME_POLICY:' || branch::text
                AND a.kind='RUNTIME_ROUTING_POLICY'
            ),'{}'::jsonb) END
        $$;
        CREATE TABLE nf_runtime_org_share (
          organization text PRIMARY KEY,
          weight integer NOT NULL DEFAULT 1 CHECK (weight BETWEEN 1 AND 100),
          virtual_finish numeric NOT NULL DEFAULT 0);
        CREATE TABLE nf_runtime_work_share (
          work_id uuid PRIMARY KEY REFERENCES nf_works,
          weight integer NOT NULL DEFAULT 1 CHECK (weight BETWEEN 1 AND 100),
          virtual_finish numeric NOT NULL DEFAULT 0);
        CREATE FUNCTION nf_runtime_available(route_scope jsonb, tokens_needed bigint,
                                             with_queued boolean) RETURNS boolean
        LANGUAGE sql STABLE AS $$
          SELECT (route_scope ? 'organization' OR NOT EXISTS
            (SELECT FROM nf_runtime_limits WHERE scope='organization')) AND NOT EXISTS (
            SELECT FROM nf_runtime_limits l WHERE l.subject=route_scope->>l.scope AND (
              (SELECT count(*) FROM nf_runtime_permits p
                WHERE NOT p.released AND p.expires_at > statement_timestamp()
                  AND p.scope->>l.scope=l.subject)
              + CASE WHEN with_queued THEN (
                SELECT count(*) FROM nf_dispatch_slots s JOIN nf_supervisor_jobs j USING(task_id) JOIN nf_tasks t USING(task_id)
                WHERE s.expires_at > statement_timestamp()
                  AND nf_runtime_scope(j.config->'runtime_scope',t.work_id,t.branch_id)->>l.scope=l.subject
                  AND NOT EXISTS (SELECT FROM nf_runtime_permits p WHERE p.task_id=s.task_id)
              ) ELSE 0 END >= l.slots
              OR (l.rpm IS NOT NULL AND (SELECT count(*) FROM nf_runtime_permits p
                WHERE p.started_at > statement_timestamp()-interval '1 minute'
                  AND p.scope->>l.scope=l.subject) >= l.rpm)
              OR (l.tpm IS NOT NULL AND (SELECT coalesce(sum(p.tokens),0) FROM nf_runtime_permits p
                WHERE p.started_at > statement_timestamp()-interval '1 minute'
                  AND p.scope->>l.scope=l.subject) + tokens_needed > l.tpm)
            ))
        $$;
        CREATE TRIGGER nf_runtime_limit_wake AFTER INSERT OR UPDATE OR DELETE ON nf_runtime_limits
          FOR EACH STATEMENT EXECUTE FUNCTION nf_signal_supervisor();
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_runtime_limits,nf_runtime_work_share,nf_runtime_org_share,nf_runtime_permits IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN IF EXISTS(SELECT FROM nf_runtime_permits) OR EXISTS(SELECT FROM nf_runtime_limits)
          OR EXISTS(SELECT FROM nf_runtime_work_share) OR EXISTS(SELECT FROM nf_runtime_org_share) THEN
          RAISE EXCEPTION 'Preserve the runtime permits and operator configuration before rollback';
        END IF; END $$;
        DROP FUNCTION nf_runtime_available(jsonb,bigint,boolean);
        DROP FUNCTION nf_runtime_scope(jsonb,uuid,uuid);
        DROP TABLE nf_runtime_work_share,nf_runtime_org_share,nf_runtime_permits,nf_runtime_limits;
    """)
