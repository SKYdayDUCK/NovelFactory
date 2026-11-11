"""Retain public employee names and independent single-occupancy staffing slots."""
from alembic import op


revision = "w001b"
down_revision = "w001a"
branch_labels = None
depends_on = None


def upgrade():
    # w001 defers these two references. Drain pending events before changing the
    # employee tables, including when an operator upgrades inside a transaction.
    references = (
        "aw_employees_org_id_supervisor_id_fkey, "
        "aw_employment_versions_org_id_org_revision_fkey"
    )
    op.execute(f"SET CONSTRAINTS {references} IMMEDIATE")
    for table in ("aw_employees", "aw_employment_versions"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN display_name text, ADD COLUMN slot_id text")
        # An original W5 database can already have the employment-history trigger.
        # This backfill only supplies public roster metadata; firing the old trigger
        # would duplicate employment events and advance every affected reader's cut.
        history_mode = None
        if table == "aw_employment_versions":
            history_mode = op.get_bind().exec_driver_sql("""
                SELECT tgenabled FROM pg_trigger WHERE tgrelid='aw_employment_versions'::regclass
                  AND tgname='aw_history' AND tgfoid=to_regprocedure('aw_employment_versions_history()')
                  AND NOT tgisinternal AND tgenabled <> 'D'
            """).scalar()
        if history_mode:
            op.execute("ALTER TABLE aw_employment_versions DISABLE TRIGGER aw_history")
        op.execute(f"UPDATE {table} SET display_name=employee_id, slot_id=employee_id")
        if history_mode:
            enable = {"O": "ENABLE", "A": "ENABLE ALWAYS", "R": "ENABLE REPLICA"}[history_mode]
            op.execute(f"ALTER TABLE aw_employment_versions {enable} TRIGGER aw_history")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN display_name SET NOT NULL, "
            "ALTER COLUMN slot_id SET NOT NULL"
        )
    op.execute(
        "CREATE UNIQUE INDEX aw_one_active_slot ON aw_employees (org_id, slot_id) "
        "WHERE status='ACTIVE'"
    )
    op.execute(f"SET CONSTRAINTS {references} DEFERRED")


def downgrade():
    raise RuntimeError("Restore a paired backup to retain slot and public-name history")
