"""Verify all changed Scion Git blobs against their identities in a restored repository."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


BASE = "23a61398278c9a13e6450d444d4a6d25a88ef5e0"
TIP = "4f249d520c921c8d6effee9978f7338b912f1dd0"
RECOVERY = "c019d971656426a5680d95d2c1bc5a35154d42fa"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def inventory(repo, left, right):
    paths = git(repo, "diff", "--name-only", "--diff-filter=ACMRT", "-z", left, right).decode().split("\0")
    wanted = set(paths) - {""}
    entries = []
    for record in git(repo, "ls-tree", "-rz", right).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        path = raw_path.decode()
        if path not in wanted:
            continue
        if kind != "blob":
            raise ValueError(f"Unsupported {kind} at {path}")
        body = git(repo, "cat-file", "blob", object_id)
        if hashlib.sha1(b"blob " + str(len(body)).encode() + b"\x00" + body).hexdigest() != object_id:
            raise ValueError(f"Git blob mismatch at {path}")
        entries.append({"path": path, "mode": mode, "git_blob": object_id, "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest()})
    if {row["path"] for row in entries} != wanted:
        raise ValueError("Restored tree missing expected paths")
    return entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="A Git repository restored from the historical refs or bundle")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    repo = args.repo
    marker = git(repo, "merge-base", BASE, TIP).decode().strip()
    changed = inventory(repo, marker, TIP)
    recovered = inventory(repo, TIP, RECOVERY)
    result = {
        "repository": "SKYdayDUCK/NovelFactory", "original_pr": 4,
        "base": BASE, "head": TIP, "merge_base": marker, "recovery": RECOVERY,
        "head_unique_commits": git(repo, "rev-list", "--reverse", f"{BASE}..{TIP}").decode().splitlines(),
        "original_refs": {"dev/p3-p4-continuity": BASE, "experiment/scion-incremental-20261111": TIP,
                          "recovery/scion-s5-evidence-20261111": RECOVERY},
        "head_changed_blobs": changed, "recovery_blobs_different_from_head": recovered,
        "head_deleted_paths": git(repo, "diff", "--name-only", "--diff-filter=D", marker, TIP).decode().splitlines(),
        "recovery_deleted_paths": git(repo, "diff", "--name-only", "--diff-filter=D", TIP, RECOVERY).decode().splitlines(),
    }
    args.manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
