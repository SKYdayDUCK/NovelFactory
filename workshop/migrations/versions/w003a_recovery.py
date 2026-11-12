"""Keep all outbound submissions fenced across restarts after a partner restore."""

import sqlalchemy as sa
from alembic import op

revision = "w003a"
down_revision = ("w003", "w002a")
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE aw_remote_receipts ADD COLUMN IF NOT EXISTS world_ref jsonb")
    op.execute("""CREATE TABLE IF NOT EXISTS aw_recovery_fences (
        installation_id text PRIMARY KEY REFERENCES aw_installations(installation_id),
        epoch bigint NOT NULL CHECK(epoch>0), frozen boolean NOT NULL,
        reason text NOT NULL, changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS aw_recovery_proofs(
        installation_id text NOT NULL REFERENCES aw_installations,
        org_id text NOT NULL, operation_id uuid NOT NULL,
        epoch bigint NOT NULL CHECK(epoch>0), ledger jsonb,
        PRIMARY KEY(installation_id,org_id,operation_id),
        FOREIGN KEY(org_id,operation_id) REFERENCES aw_delegations
    )""")
    table = "aw_delegations"
    columns = ['org_id', 'sender_id', 'sender_revision', 'client_key']
    installed = sa.inspect(op.get_bind()).get_unique_constraints(table)
    if not any(k["column_names"] == ["installation_id", *columns] for k in installed):
        (previous,) = [k for k in installed if k["column_names"] == columns]
        op.create_unique_constraint(table + "_installation_key", table, ["installation_id", *columns])
        op.drop_constraint(previous["name"], table, type_="unique")


def downgrade():
    raise RuntimeError("Restore the matching backup to preserve installation receipts and recovery decisions")
