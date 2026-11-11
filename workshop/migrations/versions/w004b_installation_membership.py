"""Join the installation-scoped W3 path with existing W4 skill and review records."""

revision = "w004b"
down_revision = ("w004a", "w003b")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Restore an isolated backup to retain both review and membership histories.")
