"""Install the signed W2 route view on databases that ran the earlier w002b merge."""

from alembic import op
from alembic.script import ScriptDirectory
from sqlalchemy import text

revision = "w004c"
down_revision = "w004b"


def upgrade():
    connection = op.get_bind()
    kind = connection.scalar(text("""
        SELECT relkind FROM pg_class
        WHERE relnamespace = current_schema()::regnamespace
          AND relname = 'aw_current_message_edges'
    """))
    if kind is None:
        # The historical w002b joined two migration branches without creating the
        # view. Replay only the signed view DDL, leaving registered rows and ACLs intact.
        scripts = ScriptDirectory.from_config(op.get_context().config)
        scripts.get_revision("w002b").module.upgrade()
    elif kind != "v":
        raise RuntimeError("aw_current_message_edges exists but is not a view")


def downgrade():
    raise RuntimeError("Restore a paired backup to retain verified default message routes")
