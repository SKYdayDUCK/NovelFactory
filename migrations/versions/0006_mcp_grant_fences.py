"""Persist a finite expiry and policy pin for branch-scoped resume credentials."""
from alembic import op
from sqlalchemy import text

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE nf_resume_grants (
            grant_id uuid PRIMARY KEY REFERENCES nf_grants(grant_id),
            expires_at timestamptz NOT NULL,
            policy_version text NOT NULL
        )
    """)


def downgrade():
    op.execute("LOCK TABLE nf_grants, nf_resume_grants IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(text("SELECT EXISTS (SELECT 1 FROM nf_grants WHERE active AND role_id='resume_operator')")):
        raise RuntimeError("Revoke resume grants before removing authorization fences")
    op.drop_table("nf_resume_grants")
