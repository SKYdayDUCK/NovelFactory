"""Join the delivered W1 directory branch and the historical W2 messaging branch."""

revision = "w002c"
down_revision = ("w001b", "w002b")
branch_labels = None
depends_on = None


def upgrade():
    # Each parent has applied its own tables, indexes and routing policy.
    pass


def downgrade():
    raise RuntimeError("Restore a paired backup to retain organization and messaging history")
