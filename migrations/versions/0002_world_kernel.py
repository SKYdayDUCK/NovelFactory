"""P1 frozen initial DDL; independent of later runtime metadata changes.

No legacy production data is rewritten. Immutable triggers deny UPDATE/DELETE.
Database owners/superusers are trusted operators, not external Agent identities.
The reviewed JSON snapshot is immutable; future changes need a new migration.
"""
from hashlib import sha256
import json
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
DDL_SHA256 = "7987fca87e59c10e7b6b595023b0b628d525f9e3b298c7a17ff5b33d19254abd"


def upgrade():
    source = Path(__file__).resolve().parents[1] / "frozen" / "0002_world_kernel.json"
    content = source.read_bytes()
    if sha256(content).hexdigest() != DDL_SHA256:
        raise RuntimeError("Frozen P1 migration bytes changed; create a new migration")
    for statement in json.loads(content)["statements"]:
        op.execute(text(statement))


def downgrade():
    raise RuntimeError("Use verified isolated backup recovery; destructive kernel downgrade is unsupported")
