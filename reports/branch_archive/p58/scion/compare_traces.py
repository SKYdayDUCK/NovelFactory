"""Check independently executed PostgreSQL traces and retain reproducible compressed witnesses."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path


SCENARIOS = ("authorization", "contention", "derived", "faults", "history", "published")


def compare(frozen_path, candidate_path):
    result = {}
    for kind in SCENARIOS:
        oracle = json.loads((frozen_path / f"{kind}.json").read_text(encoding="utf-8"))
        trial = json.loads((candidate_path / f"{kind}.json").read_text(encoding="utf-8"))
        expected = oracle["frozen"]
        for arm in trial.keys() - {"path_counts"}:
            for section in ("tables", "errors", "responses"):
                if trial[arm][section] != expected[section]:
                    raise AssertionError(f"{kind} {arm} differs from frozen runtime in {section}")
        result[kind] = {"oracle": oracle, "trial": trial}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path, help="A .json.gz file")
    args = parser.parse_args()
    witnesses = compare(args.oracle, args.candidate)
    payload = json.dumps(witnesses, sort_keys=True, separators=(",", ":")).encode()
    args.output.write_bytes(gzip.compress(payload, mtime=0))
    print(f"6 scenarios matched. Compressed witness SHA256 {hashlib.sha256(args.output.read_bytes()).hexdigest()}")
