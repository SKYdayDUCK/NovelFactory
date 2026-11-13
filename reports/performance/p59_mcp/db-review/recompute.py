"""Verify past archive digests and recompute paired results from individual observations."""

from collections import Counter
from hashlib import sha256
import gzip
import json
from math import ceil, isclose
from pathlib import Path
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
HISTORICAL = HERE.parents[1] / "p59_database" / "raw"
ARCHIVES = HISTORICAL / "actions"


def read_gzip(data):
    return json.loads(gzip.decompress(data))


def load_archived(run, archive, member):
    with ZipFile(ARCHIVES / str(run) / f"{archive}.zip") as stream:
        return read_gzip(stream.read(member))


def audit_archives():
    files = []
    for manifest_path in sorted(ARCHIVES.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        expected_files = {row["file"] for row in manifest["artifacts"]}
        assert expected_files == {path.name for path in manifest_path.parent.glob("*.zip")}
        for artifact in manifest["artifacts"]:
            path = manifest_path.parent / artifact["file"]
            payload = path.read_bytes()
            digest = sha256(payload).hexdigest()
            assert len(payload) == artifact["bytes"]
            assert artifact["digest"] == "sha256:" + digest
            with ZipFile(path) as stream:
                assert stream.testzip() is None
            files.append({"run": manifest["id"], "git": manifest["head_sha"],
                          "conclusion": manifest["conclusion"], "file": path.name,
                          "bytes": len(payload), "sha256": digest})
    return files


def percentile_ms(seconds, q):
    values = sorted(seconds)
    return round(values[ceil(len(values) * q) - 1] * 1000, 4)


def summarize(result):
    rows = result["raw"]
    if result.get("requests") is not None:
        assert {r["seq"] for r in rows} == set(range(result["requests"]))
        valid = sum(r["result"] == "verified" for r in rows)
        latencies = [r["latency_s"] for r in rows]
        renewals = [r["latency_s"] for r in rows if r["kind"] == "renew"]
    else:
        assert {r[0] for r in rows} == set(range(result["count"]))
        valid = sum(r[2] for r in rows)
        latencies = [r[3] for r in rows]
        renewals = [r[3] for r in rows if r[1] == "RENEWED"]
    rate = valid / result["seconds"]
    assert isclose(rate, result["goodput"])
    p95, p99 = (percentile_ms(latencies, p) for p in (.95, .99))
    assert (p95, p99) == (result["latency"]["p95_ms"], result["latency"]["p99_ms"])
    return {"mode": result["mode"], "valid": valid, "offered": len(rows), "goodput": rate,
            "p95_ms": p95, "p99_ms": p99, "sql": result["sql"]["sql_statements"],
            "db_peak": result["sql"]["peak_checked_out_connections"],
            "renew_p95_ms": percentile_ms(renewals, .95) if renewals else None,
            "renew_p99_ms": percentile_ms(renewals, .99) if renewals else None}


def with_pairs(rounds):
    assert len(rounds) % 2 == 0
    pairs = []
    # Sequence within each block is mirrored, with the baseline at both ends.
    for base, candidates in ((rounds[0], rounds[1:len(rounds)//2]),
                              (rounds[-1], list(reversed(rounds[len(rounds)//2:-1])))):
        assert base["mode"] == "baseline"
        for candidate in candidates:
            change = {k + "_pct": round(100 * (candidate[k] / base[k] - 1), 3)
                      for k in ("goodput", "p95_ms", "p99_ms", "sql", "renew_p95_ms", "renew_p99_ms")
                      if base[k] is not None and candidate[k] is not None}
            pairs.append({"mode": candidate["mode"], **change})
    return {"rounds": rounds, "paired_deltas": pairs}


def main():
    archives = audit_archives()
    output = {"archive_count": len(archives), "run_count": len({a["run"] for a in archives}),
              "archive_bytes": sum(a["bytes"] for a in archives), "archives": archives}
    for profile, run, archive, name in [
        ("historical_http_all", 43291814205, 13975899878, "task-http.json.gz"),
        ("historical_http_fenced", 43294749933, 13978997060, "task-http.json.gz"),
        ("historical_http_mixed_fenced", 43294749933, 13979096846, "mixed-status.json.gz"),
    ]:
        output[profile] = with_pairs([summarize(r) for r in load_archived(run, archive, name)])

    archived_mix = ARCHIVES / "43291814205" / "13978485072.zip"
    with ZipFile(archived_mix) as stream:
        names = sorted(name for name in stream.namelist() if name.startswith("comparison/")
                       and Path(name).name[:2].isdigit() and name.endswith(".json.gz"))
        output["historical_db_all"] = with_pairs([summarize(read_gzip(stream.read(n))) for n in names])
    for size in (64, 128):
        file = HISTORICAL / "task-get-boundary" / f"saturation-{size}.json.gz"
        output[f"historical_http_{size}"] = with_pairs([summarize(r) for r in read_gzip(file.read_bytes())])

    modes = ("baseline", "micro", "micro", "baseline")
    runs = [read_gzip((HERE / f"{index}-{mode}.json.gz").read_bytes())
            for index, mode in enumerate(modes, 1)]
    output["local_db"] = with_pairs([summarize(r) for r in runs])
    output["local_http_fenced"] = with_pairs([summarize(r) for r in read_gzip((HERE / "fenced.json.gz").read_bytes())])

    rows = []
    for phase in ("A1", "B1", "B2", "A2"):
        run = load_archived(43291814145, 13977741822, f"{phase}.json.gz")
        raw = run["raw_requests"]
        elapsed = run["summary"]["network_seconds"]
        valid = sum(r["valid"] for r in raw)
        assert valid == run["summary"]["business_validated"]
        latency = [r["completed_seconds"] - r["scheduled_seconds"] for r in raw]
        rows.append({"arm": phase, "verified": valid, "outcomes": dict(Counter(r["status"] for r in raw)),
                     "goodput": valid / elapsed, "p95_ms": percentile_ms(latency, .95),
                     "p99_ms": percentile_ms(latency, .99)})
    output["historical_full_timeline"] = rows
    (HERE / "audit.json").write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
