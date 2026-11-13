"""Check both adjacent comparisons in each seven-call ABBA block."""
import argparse
import gzip
import json
from pathlib import Path


def read_arm(directory, block, arm, expected_sha):
    with gzip.open(directory / f"{block}-{arm}.json.gz", "rt") as stream:
        trial = json.load(stream)
    if not trial["verified"] or trial["sha"] != expected_sha:
        raise ValueError(f"Incorrect business outcome or source for {block}-{arm}")
    return trial["phases"][1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args()
    passed = True
    for repeat in (1, 2):
        for lanes in (2, 16, 32):
            block = f"r{repeat}-{lanes}"
            for pair in (1, 2):
                a = read_arm(args.raw, block, f"a{pair}", args.baseline)
                b = read_arm(args.raw, block, f"b{pair}", args.candidate)
                ratio = b["goodput"] / a["goodput"]
                tail = {key: round(b["latency"][key] / a["latency"][key], 3)
                        for key in ("p95_ms", "p99_ms")}
                print(block, pair, round(a["goodput"], 3), round(b["goodput"], 3),
                      round(ratio, 4), tail, flush=True)
                passed &= ratio >= 1.30
    # Tail latency also requires examination of the adjacent pairs in the report.
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
