"""Join the signed W5 release, W6 installations, and early owned-roster rehearsals."""

from alembic import op
from alembic.script import ScriptDirectory


revision = "w010"
down_revision = ("w009", "w009_rehearsal", "w005c")
branch_labels = None
depends_on = None


def upgrade():
    # The first deployment of w009_rehearsal descended from w001a. Later deployments
    # used the same revision name after adding w001b. Check the physical roster
    # before applying its original, history-preserving backfill.
    count, index = (
        op.get_bind()
        .exec_driver_sql("""
        SELECT count(*), to_regclass('aw_one_active_slot')::text FROM pg_attribute
        WHERE attrelid IN ('aw_employees'::regclass,'aw_employment_versions'::regclass)
          AND attname IN ('display_name','slot_id') AND NOT attisdropped
    """)
        .one()
    )
    if count == 0 and index is None:
        scripts = ScriptDirectory.from_config(op.get_context().config)
        scripts.get_revision("w001b").module.upgrade()
    elif count != 4 or index is None:
        raise RuntimeError(
            "Incomplete roster slot columns or unique index in the deployed Workshop schema"
        )


def downgrade():
    raise RuntimeError("Restore paired backups to preserve roster, receipt and recovery history.")
