"""Join the W1 installation/membership migration and pre-existing durable W2 inbox revisions."""

revision = "w002b"
down_revision = ("w001b", "w002a")
branch_labels = None
depends_on = None


def upgrade():
    # Alembic applies only the missing branch when upgrading a W1 or historical W2 database.
    pass


def downgrade():
    raise RuntimeError("Restore an isolated backup to preserve the installation-bound inbox.")
