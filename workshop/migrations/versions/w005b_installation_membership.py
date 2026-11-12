"""Join the W4 installation-scoped reviews with the original W5 manager ledger."""

revision = "w005b"
down_revision = ("w005a", "w004b")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Restore an isolated backup to retain earlier membership and wake receipts.")
