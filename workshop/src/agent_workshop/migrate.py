"""Apply or inspect the Workshop schema from an installed wheel or checkout."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config


def config():
    package = Path(__file__).resolve().parent
    embedded = package / "migrations"
    location = embedded if embedded.is_dir() else package.parents[1] / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upgrade", "current", "heads"))
    args = parser.parse_args()
    cfg = config()
    if args.action == "upgrade":
        command.upgrade(cfg, "head")
    elif args.action == "current":
        command.current(cfg)
    else:
        command.heads(cfg)


if __name__ == "__main__":
    main()
