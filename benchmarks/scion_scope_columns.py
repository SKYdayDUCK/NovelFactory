"""Alternate each read optimization against its full reference on one PG17 snapshot."""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

from benchmarks.scion_profile import (
    Attribution,
    hardware,
    isolated_kernel,
    make_population,
    normalized_query,
)
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.templates import build
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import point


def run(output, pairs):
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src"], cwd=root, check=True)
    with tempfile.TemporaryDirectory() as objects, isolated_kernel(
            os.environ["NF_TEST_DATABASE_URL"], Path(objects)) as (kernel, _url):
        fixture = make_population(kernel, 200, .8)
        world, ids = fixture["world"], fixture["ids"]
        actor = next(row for row in fixture["population"] if not row["writer"])
        wr = world["world_ref"]
        with kernel.db.uow(write=False) as repo:
            pg = repo.conn.scalar(text("SHOW server_version"))
            assert pg.startswith("17.")
            revision = kernel.revision(repo, wr)
            cmd = {"from_account": ids["reserve"], "to_account": ids["recipient"],
                   "resource_id": ids["grain"], "quantity": "1", "unit": "bag", "at": point(6)}
            footprint = build("RESOURCE_TRANSFER", cmd, revision["snapshot"], wr["world_revision_id"])
            work = repo.one("works", {"work_id": wr["work_id"]})
            grant = repo.one("grants", {"grant_id": world["principal"].grant_id})
            branch = repo.one("branches", {"branch_id": wr["branch_id"]})
        service = WorldQueryService(kernel)
        service.query(actor["principal"], actor["state"])
        samples, reference = [], {}
        with Attribution(kernel) as meter:
            for pair in range(pairs):
                for operation in ("state_query_warm", "worker_source_recheck", "certificate_tokens"):
                    for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
                        kernel.scion.columns = enabled and operation != "certificate_tokens"
                        kernel.scion.scopes = enabled and operation == "certificate_tokens"
                        meter.reset()
                        meter.active = True
                        start, cpu = time.perf_counter_ns(), time.process_time_ns()
                        try:
                            if operation == "state_query_warm":
                                value = normalized_query(service.query(actor["principal"], actor["state"]))
                            else:
                                with kernel.db.uow(write=False) as repo:
                                    if operation == "worker_source_recheck":
                                        keys = ("branch_seq", "state_hash", "manifest_hash")
                                        row = repo.one("revisions", wr, columns=keys if enabled else None)
                                        value = {key: row[key] for key in keys}
                                    else:
                                        value = kernel._tokens(repo, wr, footprint, grant, [],
                                                               branch["history_epoch"], work,
                                                               template_id="RESOURCE_TRANSFER")
                        finally:
                            wall_ns = time.perf_counter_ns() - start
                            cpu_ns = time.process_time_ns() - cpu
                            meter.active = False
                        result_hash = fingerprint(list(value) if isinstance(value, tuple) else value)
                        assert reference.setdefault(operation, result_hash) == result_hash
                        samples.append({
                            "operation": operation, "pair": pair, "enabled": enabled,
                            "wall_ns": wall_ns, "process_cpu_ns": cpu_ns,
                            "sql_calls": sum(meter.sql.values()), "rows": meter.selected_rows,
                            "decoded_json_bytes": meter.json_stats["decode"]["bytes"],
                            "result_sha256": result_hash,
                        })
        evidence = {
            "source_sha": source, "postgres": pg, "environment": hardware(), "samples": samples,
            "fixture": {"world_ref": wr, "state_hash": revision["state_hash"],
                        "selection": len(revision["selection"]), "scope_count": len(revision["scope_versions"]),
                        "footprint_keys": len(set(footprint["reads"] + footprint["writes"]))},
            "note": "Real query, token builder and worker source SELECT. Worker SELECT excludes worker materialization.",
        }
        output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=7)
    args = parser.parse_args()
    run(args.output, args.pairs)
