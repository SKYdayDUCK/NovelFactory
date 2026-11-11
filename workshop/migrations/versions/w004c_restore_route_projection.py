"""Restore the signed W2 route projection on databases from the old W4 revision graph."""
from alembic import op
from alembic.script import ScriptDirectory
import sqlalchemy as sa

revision = "w004c"
down_revision = "w004b"
branch_labels = None
depends_on = None


def upgrade():
    # The historic w004b already included W1 ownership and W3 recovery, but
    # its w002b was an empty merge. The canonical w002b also creates this view.
    if not sa.inspect(op.get_bind()).has_table("aw_current_message_edges"):
        scripts = ScriptDirectory.from_config(op.get_context().config)
        scripts.get_revision("w002b").module.upgrade()


def downgrade():
    raise RuntimeError("Restore a paired backup to retain verified default message routes")
