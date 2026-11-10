"""Run ``python -m novel_factory.contracts.generate [--check]`` at repository root.

This maintenance tool is deliberately separate from the pure runtime registry.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import pprint
import sys


def generated(root: Path) -> str:
    names = sorted(path.name for path in (root / 'contracts').glob('*.schema.json'))
    schemas = {name: json.loads((root / 'contracts' / name).read_text()) for name in names}
    hashes = {name: hashlib.sha256((root / 'contracts' / name).read_bytes()).hexdigest() for name in names}
    return ('# Generated from contracts/*.schema.json. Do not edit by hand.\n'
            + 'SOURCE_SHA256 = ' + repr(hashes) + '\n'
            + 'SCHEMAS = ' + pprint.pformat(schemas, sort_dicts=True, width=110) + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    destination = args.root / 'src/novel_factory/contracts/_schema_bundle.py'
    content = generated(args.root)
    if args.check:
        if not destination.is_file() or destination.read_text() != content:
            print('Schema bundle drift; regenerate before accepting changes.', file=sys.stderr)
            return 1
        print('Schema source and generated bundle match exactly.')
    else:
        destination.write_text(content)
        print('Generated source bundle from all maintained schemas.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
