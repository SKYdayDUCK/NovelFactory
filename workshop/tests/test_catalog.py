import json
from pathlib import Path

from agent_workshop.server import catalog_binding


def test_negotiated_catalog_identifies_the_frozen_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "contracts" / "nf-catalog.json").read_text())
    assert len(manifest["roles"]) == 10
    assert len(manifest["skills"]) == 12
    assert catalog_binding() == {
        "nf_commit": manifest["nf_commit"],
        "role_catalog_hash": manifest["catalogs"]["runtime/roles/catalog.json"],
        "skill_catalog_hash": manifest["catalogs"]["runtime/skills/catalog.json"],
    }
