"""Expose the current permitted default routes without rewriting frozen W1 graphs."""
from alembic import op

revision = "w002b"
down_revision = "w002a"


def upgrade():
    op.execute("""
        CREATE VIEW aw_current_message_edges AS
        SELECT e.org_id,e.graph_revision,e.src,e.dst,e.kind,e.scope_ref
        FROM aw_edges e
        JOIN aw_organizations o ON (o.org_id,o.active_revision)=(e.org_id,e.graph_revision)
        JOIN aw_employees src ON (src.org_id,src.employee_id)=(e.org_id,e.src)
        JOIN aw_employees dst ON (dst.org_id,dst.employee_id)=(e.org_id,e.dst)
        WHERE src.status='ACTIVE' AND dst.status='ACTIVE'
          AND NOT (src.role_id='reviewer' AND dst.role_id='review_lead' AND e.kind='ASK')
    """)


def downgrade():
    op.execute("DROP VIEW aw_current_message_edges")
