"""Verify the wheel's MCP catalog and apply its packaged migrations in the CI database."""

import argparse
import hashlib
import json
import os
import sys
from importlib.metadata import distribution
from pathlib import Path

import psycopg
from alembic import command
from alembic.script import ScriptDirectory

from agent_workshop import migrate, server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()

    package = Path(distribution("agent-workshop").locate_file("agent_workshop")).resolve()
    if not package.is_relative_to(Path(sys.prefix)) or Path(server.__file__).resolve().parent != package:
        raise RuntimeError("The MCP server must load from the isolated wheel installation")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = {
        "nf_commit": manifest["nf_commit"],
        "role_catalog_hash": manifest["catalogs"]["runtime/roles/catalog.json"],
        "skill_catalog_hash": manifest["catalogs"]["runtime/skills/catalog.json"],
    }
    actual = server.catalog_binding()
    if actual != expected:
        raise RuntimeError(f"Installed catalog disagrees with the checked-in NF binding: {actual!r}")
    cfg = migrate.config()
    scripts = ScriptDirectory.from_config(cfg)
    if Path(scripts.dir).resolve() != package / "migrations":
        raise RuntimeError("Migrations must load from the installed wheel")
    head = scripts.get_current_head()
    command.upgrade(cfg, "head")

    with psycopg.connect(os.environ["AW_DATABASE_URL"]) as connection:
        [(revision,)] = connection.execute("SELECT version_num FROM aw_alembic_version").fetchall()
    if not head or revision != head:
        raise RuntimeError(f"Database revision {revision!r} differs from packaged head {head!r}")
    print(json.dumps({
        "wheel": args.wheel.name,
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "installed_package": str(package.relative_to(Path(sys.prefix))),
        "catalog": actual,
        "migration_head": head,
        "database_revision": revision,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
