"""Retain verified sender credentials across inbox delivery and acknowledgement."""

from alembic import op

revision = "w002a"
down_revision = "w002"


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS aw_message_origins (
        org_id text NOT NULL, message_id uuid NOT NULL, token_digest char(64) NOT NULL REFERENCES aw_bindings,
        grant_epoch bigint NOT NULL, PRIMARY KEY(org_id,message_id), FOREIGN KEY(org_id,message_id) REFERENCES aw_messages
      );
    """)


def downgrade():
    raise RuntimeError("Restore the matching backup to retain message origin history")
