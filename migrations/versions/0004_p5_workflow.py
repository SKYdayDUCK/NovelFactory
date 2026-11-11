"""Attach wait groups and prerequisite tasks to immutable TaskSpec generations."""
from alembic import op
from sqlalchemy import text

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_workflow_waits (
            task_id uuid NOT NULL,
            generation integer NOT NULL,
            mode varchar(3) NOT NULL CONSTRAINT ck_workflow_wait_mode CHECK (mode IN ('AND','OR')),
            grant_epoch bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            woken_at timestamptz,
            PRIMARY KEY (task_id, generation),
            FOREIGN KEY (task_id, generation) REFERENCES nf_task_inputs (task_id, generation)
        )
    """)
    op.execute("""
        CREATE TABLE nf_workflow_edges (
            task_id uuid NOT NULL,
            generation integer NOT NULL,
            predecessor_id uuid NOT NULL REFERENCES nf_tasks (task_id),
            PRIMARY KEY (task_id, generation, predecessor_id),
            FOREIGN KEY (task_id, generation) REFERENCES nf_workflow_waits (task_id, generation),
            CONSTRAINT ck_workflow_edge_distinct CHECK (task_id <> predecessor_id)
        )
    """)


def downgrade():
    op.execute("LOCK TABLE nf_workflow_waits, nf_workflow_edges IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM nf_workflow_waits)")):
        raise RuntimeError("Downgrade would discard workflow evidence. Retain schema or restore a pre-P5 backup.")
    op.drop_table("nf_workflow_edges")
    op.drop_table("nf_workflow_waits")
