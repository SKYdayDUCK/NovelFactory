"""Configure durable admission and freeze the initial queue deadline at commit."""
from alembic import op

revision = "0023_hera_admission"
down_revision = "0022_quota_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_durable_admission_policy (
            singleton boolean PRIMARY KEY CHECK (singleton),
            enabled boolean NOT NULL DEFAULT false,
            total_limit integer NOT NULL CHECK (total_limit > 0),
            work_limit integer NOT NULL CHECK (work_limit > 0),
            principal_limit integer NOT NULL CHECK (principal_limit > 0),
            resource_limit integer NOT NULL CHECK (resource_limit > 0),
            max_queue_seconds integer NOT NULL CHECK (max_queue_seconds > 0),
            max_age_seconds integer NOT NULL CHECK (max_age_seconds >= max_queue_seconds),
            role_resources jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(role_resources)='object'
                AND NOT jsonb_path_exists(role_resources, '$.* ? (@ != "MODEL" && @ != "CPU_COMPUTE" && @ != "DB_READ_LIGHT" && @ != "DB_READ_HEAVY" && @ != "DB_WRITE" && @ != "MAINTENANCE" && @ != "NOTIFICATION")'))
        );
        INSERT INTO nf_durable_admission_policy
            (singleton,total_limit,work_limit,principal_limit,resource_limit,max_queue_seconds,max_age_seconds)
        VALUES (true,10000,5000,1000,10000,3600,86400);
        CREATE TABLE nf_durable_admissions (
            task_id uuid PRIMARY KEY REFERENCES nf_tasks,
            resource_class text NOT NULL,
            queue_deadline timestamptz NOT NULL
        );
        CREATE INDEX nf_durable_queue_deadline ON nf_durable_admissions(queue_deadline,task_id)
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_durable_admission_policy,nf_durable_admissions IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT FROM nf_durable_admission_policy WHERE enabled)
                OR EXISTS (SELECT FROM nf_durable_admissions a JOIN nf_tasks t USING(task_id)
                           WHERE t.state IN ('READY','BLOCKED','PAUSED')
                             AND NOT EXISTS (SELECT FROM nf_task_executions e WHERE e.task_id=t.task_id)) THEN
                RAISE EXCEPTION 'Disable durable admission and drain initial waits before schema rollback';
            END IF;
        END $$;
        DROP TABLE nf_durable_admissions,nf_durable_admission_policy;
    """)
