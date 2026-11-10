"""P0: immutable contract baseline registry; no existing data to backfill.

Upgrade takes only DDL locks on a new table. Downgrade is intentionally refused:
use an isolated database or restore a verified database/object backup.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nf_contract_baseline",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
    )
    # Original source bytes, frozen in the supplied design package.
    from pathlib import Path
    import hashlib
    root = Path(__file__).resolve().parents[2]
    rows = [{"name": n, "sha256": hashlib.sha256((root / "contracts" / n).read_bytes()).hexdigest(),
             "schema_version": "2.1" if n.startswith("temporal") else "2.0"}
            for n in ("core.schema.json", "literary.schema.json", "temporal_concurrency.schema.json")]
    table = sa.table("nf_contract_baseline", sa.column("name"), sa.column("sha256"), sa.column("schema_version"))
    op.bulk_insert(table, rows)


def downgrade():
    raise RuntimeError("Destructive downgrade is not a recovery strategy; restore an isolated backup")
