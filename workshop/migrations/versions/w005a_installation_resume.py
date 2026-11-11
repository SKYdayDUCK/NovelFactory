"""Namespace user request keys by the server-authenticated installation."""

from alembic import op
import sqlalchemy as sa

revision = "w005a"
down_revision = ("w005", "w004a")


def upgrade():
    for table, columns in (
        ("aw_resume_requests", ["org_id", "employee_id", "employment_revision", "operation_id"]),
    ):
        keys = sa.inspect(op.get_bind()).get_unique_constraints(table)
        if any(key["column_names"] == ["installation_id", *columns] for key in keys):
            continue
        (original,) = [key for key in keys if key["column_names"] == columns]
        op.create_unique_constraint(
            table + "_installation_key", table, ["installation_id", *columns]
        )
        op.drop_constraint(original["name"], table, type_="unique")


def downgrade():
    raise RuntimeError("Restore the matching backup to preserve installation-scoped request keys.")
