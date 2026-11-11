"""Bring existing W3 recovery databases and W2 installation ownership into one revision."""

revision = "w003b"
down_revision = ("w003a", "w002c")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Restore a paired backup to preserve both authorization and recovery histories.")
