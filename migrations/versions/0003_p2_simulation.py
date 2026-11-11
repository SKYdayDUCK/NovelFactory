"""P2 additive immutable interaction candidates; no P1 records are rewritten."""
from hashlib import sha256
import json
from pathlib import Path
from alembic import op
from sqlalchemy import text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None
DDL_SHA256 = "c5e30e7d3169079d3298ab8cf2ef4e091f253d9fb3920dff69dce4e70f73c651"


def upgrade():
    source = Path(__file__).resolve().parents[1] / "frozen" / "0003_p2_simulation.json"
    content = source.read_bytes()
    if sha256(content).hexdigest() != DDL_SHA256:
        raise RuntimeError("Frozen P2 migration bytes changed; create a new migration")
    for statement in json.loads(content)["statements"]:
        op.execute(text(statement))


def downgrade():
    raise RuntimeError("Preserve sealed inputs and outcomes; use verified isolated backup recovery")
