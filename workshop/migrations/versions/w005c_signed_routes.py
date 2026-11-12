"""Join the deployed W5 ledger with the signed W4 routing and review schema."""

revision = "w005c"
down_revision = ("w005b", "w004c")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    raise RuntimeError("Restore a paired backup to retain the ledger and historical route grants")
