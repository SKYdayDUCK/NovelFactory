"""Build the shareable NF catalog manifest from an identified Git revision."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def freeze(root):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])
    revision = git("rev-parse", "HEAD").decode().strip()
    manifest = {"nf_commit": revision, "catalogs": {}, "roles": {}, "skills": {}}
    for group, name, prop in (("roles", "role_id", "contract_path"), ("skills", "name", "path")):
        path = f"runtime/{group}/catalog.json"
        raw = git("show", revision + ":" + path)
        manifest["catalogs"][path] = hashlib.sha256(raw).hexdigest()
        for entry in json.loads(raw)[group]:
            item = f"runtime/{group}/" + entry[prop]
            data = git("show", revision + ":" + item)
            manifest[group][entry[name]] = {"path": item, "sha256": hashlib.sha256(data).hexdigest()}
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nf", type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.nf), indent=2, sort_keys=True))
