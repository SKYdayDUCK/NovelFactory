"""Verify a downloaded Agent Workshop workflow run offline.

Save the GitHub REST responses for actions/runs/{id}, its jobs and its artifacts
as run.json, jobs.json and artifacts.json. Request jobs with filter=all. Collect
all pages of jobs and artifacts, preserving each total_count. Supply the original named ZIPs from
that run and the expected checkout SHA (GITHUB_SHA, the merge ref on a PR).
Captured GitHub metadata is the trust root for the archive digests.
"""

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

JOBS = {
    "workshop-results": "workshop",
    "workshop-scene-recovery-results": "W6 message capacity and end-to-end scenes",
    "workshop-comparisons": "Matched admission and extended scene presets",
}
SIZES = (10, 50, 200)


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def read_json(path):
    return json.loads(path.read_bytes())


def verify_run(run, checkout_sha):
    require(run["repository"]["full_name"] == "SKYdayDUCK/NovelFactory", "wrong repository")
    require(run["path"] == ".github/workflows/agent-workshop.yml", "wrong workflow")
    require(
        run["status"] == "completed" and run["conclusion"] == "success", "workflow did not pass"
    )
    require(re.fullmatch(r"[0-9a-f]{40}", checkout_sha), "supply a full checkout SHA")
    require(run["event"] in {"pull_request", "push"}, "unsupported trigger")
    if run["event"] == "push":
        require(
            run["head_branch"] == "main" and run["head_sha"] == checkout_sha,
            "push checkout differs from main",
        )


def all_entries(path, field):
    listing = read_json(path)
    entries = listing[field]
    require(len(entries) == listing["total_count"], f"{field}: incomplete GitHub pagination")
    return entries


def effective_job(run, jobs, name):
    matching = [job for job in jobs if job["name"] == name]
    require(matching, f"missing job: {name}")
    for job in matching:
        require(job["run_id"] == run["id"], f"{name}: mixed run IDs")
        require(job["head_sha"] == run["head_sha"], f"{name}: mixed workflow heads")
        require(1 <= job["run_attempt"] <= run["run_attempt"], f"{name}: wrong job attempt")
    latest_attempt = max(job["run_attempt"] for job in matching)
    latest = [job for job in matching if job["run_attempt"] == latest_attempt]
    require(len(latest) == 1, f"{name}: ambiguous executions at attempt {latest_attempt}")
    job = latest[0]
    require(
        job["status"] == "completed" and job["conclusion"] == "success",
        f"{name}: latest job attempt {latest_attempt} did not pass",
    )
    return job


def verify_archive(path, run, job, artifacts):
    name = path.stem
    matching = [artifact for artifact in artifacts if artifact["name"] == name]
    start = datetime.fromisoformat(job["started_at"])
    end = datetime.fromisoformat(job["completed_at"])
    candidates = []
    for artifact in matching:
        origin = artifact["workflow_run"]
        require(origin["id"] == run["id"], f"{name}: mixed run IDs")
        require(origin["repository_id"] == run["repository"]["id"], f"{name}: wrong repository ID")
        require(origin["head_sha"] == run["head_sha"], f"{name}: mixed workflow heads")
        if start <= datetime.fromisoformat(artifact["created_at"]) <= end:
            candidates.append(artifact)
    require(len(candidates) == 1, f"{name}: missing, ambiguous or archive outside job attempt")
    artifact = candidates[0]
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(artifact["digest"] == "sha256:" + digest, f"{name}: archive digest mismatch")
    return {
        "artifact_id": artifact["id"],
        "job_id": job["id"],
        "job_attempt": job["run_attempt"],
        "sha256": digest,
    }, raw


def member(archive, name):
    try:
        return json.loads(archive.read(name))
    except (KeyError, ValueError) as error:
        raise ValueError(f"{name}: {error}") from error


def check_services(archive, sha):
    dual = member(archive, "codex-dual/receipt.json")
    require(dual["workshop_sha"] == dual["nf_sha"] == sha, "dual MCP checkout mismatch")
    require(
        dual["separate_service_clusters"]
        and dual["separate_service_uids"]
        and dual["aw_postgres_system_identifier"] != dual["nf_postgres_system_identifier"],
        "dual MCP services did not use distinct PostgreSQL clusters and identities",
    )
    require(
        dual["model_turns"] == 0
        and dual["cross_service_binding_reads_denied"] == 2
        and dual["task_identity_matched"]
        and dual["employee_roster_matched"],
        "dual MCP binding mismatch",
    )

    wal = member(archive, "paired-restore.json")
    require(
        wal["status"] == "PASS" and wal["verification"]["exit_code"] == 0,
        "physical backup verification",
    )
    require(
        wal["replayed_lsn"] == wal["target_lsn"]
        and wal["second_replay_lsn"] == wal["second_target_lsn"]
        and wal["target_lsn"] != wal["second_target_lsn"],
        "distinct WAL replay targets",
    )
    mismatch = wal["nf_restored_aw_live"]
    require(
        mismatch["last_error"] == "REMOTE_HISTORY_MISMATCH" and mismatch["hold"] == "HELD",
        "restored NF history mismatch did not preserve the hold",
    )
    pending = wal["aw_restored_nf_live_before_invoice"]
    settled = wal["after_explicit_synthetic_invoice"]
    require(
        pending["accounting_state"] == "UNKNOWN"
        and pending["hold"] == "HELD"
        and settled["hold"] == "SETTLED"
        and settled["receipt_ref"] == pending["receipt_ref"]
        and wal["resulting_nf_tasks"] == 1
        and wal["supplier_model_calls"] == 0,
        "restored ledger reconciliation",
    )

    redis = member(archive, "w3-redis-recovery.json")
    before, after = redis["before_loss"], redis["after_rebuild"]
    require(
        before["run_id"] != after["run_id"]
        and after["initial_keys"] == 0
        and after["aw_event_id"] == before["aw_event_id"]
        and set(after["nf_task_events"]) == set(before["nf_task_events"])
        and bool(before["nf_task_events"])
        and redis["hold_after_loss"] == "HELD"
        and redis["nf_after_get_matches_lost_reply"],
        "fresh Redis replay or lost-reply reconciliation",
    )
    return {
        "postgres_system_ids": [
            dual["aw_postgres_system_identifier"],
            dual["nf_postgres_system_identifier"],
        ],
        "wal_targets": [wal["target_lsn"], wal["second_target_lsn"]],
        "redis_replayed_operation": redis["operation_id"],
    }


def check_messages(archive, sha, size):
    data = member(archive, f"ci-evidence/messages-{size}.json")
    require(data["started_workshop_sha"] == sha, f"messages-{size}: checkout mismatch")
    count = data["scheduled_messages"]
    require(
        data["message_gate"] == "PASS"
        and data["employees"] == size
        and data["rounds"] > 0
        and count == size * data["rounds"] == data["unique_completed_exchanges"],
        f"messages-{size}: coverage",
    )
    receipts = data["receipt_counts"]
    replay = data["notification_loss_and_restart"]
    require(
        all(
            receipts[k] == count
            for k in ("messages", "acknowledged", "pending_hints", "durable_inbox")
        )
        and all(
            replay[k] == count for k in ("replayed_outbox", "rebuilt_stream", "distinct_consumed")
        ),
        f"messages-{size}: durable exchange and Redis rebuild counts",
    )
    return count


def check_delegations(archive, size, policy):
    suffix = "" if policy == "fixed-1" else "-" + policy
    case = f"delegations-{size}{suffix}"
    data = member(archive, f"ci-evidence/{case}.json")
    require(
        data["policy"] == policy and data["provider"] == "local HTTP synthetic",
        f"{case}: wrong workload",
    )
    require(
        all(
            data[field] == size
            for field in (
                "registered_members",
                "delegations",
                "tasks",
                "unique_tasks",
                "accepted",
                "settled",
                "provider_requests",
            )
        )
        and data["held"]
        == data["extra_budget_authorizations"]
        == data["model_calls_while_idle"]
        == 0,
        f"{case}: admission, settlement or fixture call counts",
    )
    return data["accepted"]


def check_scene(archive, sha, preset):
    scene = member(archive, f"ci-evidence/scene-{preset.lower()}.json")
    require(scene["preset"] == preset, f"{preset}: wrong scene")
    for service in ("NovelFactory", "Workshop"):
        source = scene["source"][service]
        require(
            source["head"] == sha and source["working_changes"] == [],
            f"{preset}: {service} checkout mismatch",
        )
    review = scene["independent_workshop_review"]
    frozen = scene["reviewed_frozen_source"]
    prose = scene["prose"]["text_ref"]
    require(
        scene["reviewer"] != scene["maintainer"]
        and review["state"] == "APPROVED"
        and review["review_id"]
        and frozen["artifact_revision"] == prose["artifact_revision_id"]
        and frozen["content_sha"] == prose["sha256"],
        f"{preset}: independent frozen-text review",
    )
    mail = scene["production_mail"] + scene["author_retrieval"]
    require(
        scene["production_mail"]
        and scene["author_retrieval"]
        and all(
            m["receipt"]["status"] == "ACKNOWLEDGED"
            and m["receipt"]["message_id"] == m["message_id"]
            for m in mail
        ),
        f"{preset}: staff or author missing message acknowledgement",
    )
    progress = scene["author_task_progress"]
    require(
        progress["task_id"] == scene["delegated_task_id"]
        and progress["accounting_state"] == "UNKNOWN"
        and progress["hold"] == "HELD"
        and scene["hold"]["status"] == "SETTLED"
        and {entry["state"] for entry in scene["cost_observations"]} == {"UNKNOWN", "SUCCEEDED"},
        f"{preset}: unknown hold and settlement",
    )
    return {"operation_id": scene["operation_id"], "review_id": review["review_id"]}


def verify(metadata_dir, archives, checkout_sha):
    run = read_json(metadata_dir / "run.json")
    verify_run(run, checkout_sha)
    jobs = all_entries(metadata_dir / "jobs.json", "jobs")
    artifacts = all_entries(metadata_dir / "artifacts.json", "artifacts")
    result = {
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "event": run["event"],
        "head_sha": run["head_sha"],
        "checkout_sha": checkout_sha,
        "archives": {},
        "messages": {},
        "delegations": {},
        "scenes": {},
        "literary_quality": "NOT_EVALUATED",
        "historical_nf_capacity": "NOT_EVALUATED_BY_THIS_RUN",
    }
    for name, job_name in JOBS.items():
        path = archives / (name + ".zip")
        job = effective_job(run, jobs, job_name)
        receipt, raw = verify_archive(path, run, job, artifacts)
        result["archives"][name] = receipt
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if name == "workshop-results":
                result["services_and_recovery"] = check_services(archive, checkout_sha)
                continue
            if name == "workshop-scene-recovery-results":
                result["messages"] = {
                    size: check_messages(archive, checkout_sha, size) for size in SIZES
                }
                policies, presets = ("fixed-1",), ("Minimal", "Standard")
            else:
                policies, presets = ("fixed-2", "adaptive-2"), ("Long-form", "Research-Audit")
            for policy in policies:
                result["delegations"][policy] = {
                    size: check_delegations(archive, size, policy) for size in SIZES
                }
            for preset in presets:
                result["scenes"][preset] = check_scene(archive, checkout_sha, preset)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--checkout-sha", required=True)
    args = parser.parse_args()
    try:
        evidence = verify(args.metadata_dir, args.archives, args.checkout_sha)
    except (OSError, ValueError, KeyError, IndexError, TypeError, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        return 1
    print(json.dumps({"status": "PASS", **evidence}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
