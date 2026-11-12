"""Index the parent draft of an immutable manuscript revision.

0018 -> 0019. One scan of nf_artifacts, retaining entries for narrative drafts
only. The transactional build takes a SHARE table lock for the scan; schedule
the migration in a write maintenance window on large installations. Readers
remain available. A failed migration rolls back. Inspect pg_indexes for
nf_draft_prior_revision after upgrade. Downgrade removes only the derived
index, leaving every artifact and its history readable by the earlier code.
"""
from alembic import op

revision = "0019_p57_context_sources"
down_revision = "0018_p6_shared_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE INDEX nf_draft_prior_revision ON nf_artifacts
            (work_id, (payload #>> '{prior_draft_ref,artifact_revision_id}'))
        WHERE kind = 'NARRATIVE_DRAFT'
    """)


def downgrade():
    op.execute("DROP INDEX nf_draft_prior_revision")
