"""Observe one enrolled production batch from its persistent Linux Worker host (read-only)."""

import argparse
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid5

from bootstrap_p5_m0 import save
from p6_batch import BOOKS, MODELS, ROOT, private
from p6_deepseek_costs import summarize as price_exports
from p6_deepseek_costs import transport
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from novel_factory.application.kernel import Kernel, require
from novel_factory.application.production_extent import _count_han
from novel_factory.application.rolling_production import _load as load_rolling
from novel_factory.application.runtime_evidence import export_runtime_evidence
from novel_factory.application.statistics import is_official_deepseek
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.native_files import private_export
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.private_files import prepare_private_directory
from novel_factory.infrastructure.wire_receipts import read_receipt

MILESTONES = (0, 120, 300, 600)


def utc():
    return datetime.now(UTC)


def worker_active(project):
    """An atomic nonblocking probe of the same POSIX lock held throughout serve()."""
    import fcntl

    try:
        fd = os.open(project / ".worker.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def scopes(kernel, project, registration):
    access = private(project / "preparation.json")
    wr = access["world_ref"]
    require(
        all(wr[k] == registration[k] for k in ("work_id", "branch_id")),
        "INVALID_EVIDENCE",
        "Preparation identity changed",
    )
    yield "preparation", wr, kernel.authenticate(access["token"])
    if (project / "first-window.json").exists():
        access = private(project / "supervisor.json")
        target = str(uuid5(UUID(wr["work_id"]), "literary-work"))
        require(access["work_id"] == target, "INVALID_EVIDENCE", "Unrelated literary work")
        handoff = private(project / "first-window.json")["world_ref"]
        require(
            all(access[k] == handoff[k] for k in ("work_id", "branch_id")),
            "INVALID_EVIDENCE",
            "Literary handoff differs",
        )
        yield "writing", handoff, kernel.authenticate(access["token"])


def is_official(call):
    return (
        (call.get("call_origin"), call.get("adapter"), call.get("provider"))
        == ("PROVIDER", "deepseek", "api.deepseek.com")
        and call.get("model") in MODELS.values()
        and call.get("execution_kind") == "REAL_EXTERNAL"
        and call.get("official_deepseek_endpoint") is True
    )


def corroborated_spans(store, rows):
    """Bind each counted intersection to bytes written by the isolated HTTP worker."""
    output = []
    for row in rows:
        span = transport(row)
        if not span or not is_official(row):
            continue
        saved = read_receipt(store, row["attempt_id"], row["request_hash"])
        if saved is None:
            continue
        receipt, body = saved
        if not receipt["complete"] or receipt["http_status"] != 200:
            continue
        try:
            decoded = json.loads(body)
            window = receipt["transport_window"]
            worker_span = transport({"sent_at": window["started_at"], "received_at": window["received_at"]})
            receipt_id = receipt.get("receipt") or decoded.get("id")
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if not isinstance(decoded, dict):
            continue
        if (worker_span == span and decoded.get("model") == row["model"] and isinstance(receipt_id, str)
                and sha256(receipt_id.encode()).hexdigest() == row["provider_receipt_sha256"]):
            output.append((row["attempt_id"], *span))
    return output


def overlap(books, start, end):
    """Count distinct active books using half-open, clipped response transport intervals."""
    events = defaultdict(Counter)
    for book, spans in books.items():
        for _, a, b in spans:
            a, b = max(a, start), min(b, end)
            if a < b:
                events[a][book] += 1
                events[b][book] -= 1
    active = Counter()
    previous = start
    triple = 0.0
    verified = []
    peak = 0
    for stamp in sorted(events):
        n = sum(x > 0 for x in active.values())
        if stamp > previous:
            peak = max(peak, n)
            if n == 3:
                triple += (stamp - previous).total_seconds()
                verified.append([previous.isoformat(), stamp.isoformat()])
        active.update(events[stamp])
        previous = stamp
    return {
        "distinct_books_peak_observed": peak,
        "verified_three_book_seconds": triple,
        "three_book_intervals": verified,
        "all_three_overlap_proven": triple > 0,
    }


def frozen_extent(kernel, repo, work, branch):
    cursor = load_rolling(kernel, repo, work, branch)
    ref = cursor["last_checkpoint"] if cursor else None
    seen, chapters, scenes = set(), set(), set()
    characters = 0
    while ref:
        require(ref["sha256"] not in seen, "INVALID_EVIDENCE", "Cyclic checkpoint")
        seen.add(ref["sha256"])
        cp = kernel.artifact(repo, ref, work, kind="ROLLING_CHECKPOINT")["payload"]
        require(
            fingerprint(cp) == ref["sha256"]
            and cp["world_ref"]["branch_id"] == branch
            and cp["world_ref"]["work_id"] == work
            and cp["run_id"] == cursor["run_id"],
            "INVALID_EVIDENCE",
            "Checkpoint outside selected rolling run",
        )
        for chapter in cp["chapters"]:
            require(chapter["chapter_id"] not in chapters, "INVALID_EVIDENCE", "Duplicate chapter")
            chapters.add(chapter["chapter_id"])
            for scene in chapter["scenes"]:
                require(scene["scene_id"] not in scenes, "INVALID_EVIDENCE", "Duplicate scene")
                scenes.add(scene["scene_id"])
                digest = scene["text_ref"]["sha256"]
                data = kernel.objects.path(digest).read_bytes()
                require(
                    sha256(data).hexdigest() == digest, "INVALID_EVIDENCE", "Text digest mismatch"
                )
                characters += _count_han(data.decode("utf-8"))
        ref = cp["previous"]
    require(
        len(chapters) == (cursor["frozen_chapters"] if cursor else 0),
        "INVALID_EVIDENCE",
        "Selected chapter count differs",
    )
    return {
        "chapters": len(chapters),
        "scenes": len(scenes),
        "han": characters,
        "rolling_state": cursor["state"] if cursor else None,
        "target_chapters": cursor["target_chapters"] if cursor else None,
    }


_SENT = """
SELECT DISTINCT c.attempt_id::text FROM nf_tasks t
JOIN nf_call_attempts c ON c.task_id=t.task_id AND c.generation=t.generation
JOIN nf_supervisor_jobs j ON j.task_id=t.task_id
JOIN nf_task_inputs i ON i.task_id=t.task_id AND i.generation=t.generation
JOIN LATERAL jsonb_array_elements(i.spec->'input_artifacts') x ON true
JOIN nf_artifacts a ON a.work_id=t.work_id AND a.artifact_revision_id=(x->>'artifact_revision_id')::uuid
 AND a.sha256=x->>'sha256' AND a.kind='AGENT_INPUT' AND a.payload->>'role_id'=t.role_id
WHERE t.work_id=:work AND t.branch_id=:branch AND c.state='SENT'
 AND t.state='RUNNING' AND t.lease_expiry>clock_timestamp()
 AND a.payload->>'model_config_sha256' IN (:creative,:review)
"""

_MEMORY = """
SELECT kind, payload->>'error_type' AS error_type, payload->>'phase' AS phase,
       payload->>'decision' AS decision, count(*) AS n
FROM nf_artifacts WHERE work_id=:work
 AND kind IN ('P6_ERROR_RECORD','P6_REFLECTION_RECORD','P6_EXPERIENCE_MEMORY')
 AND coalesce(payload->>'branch_id',payload#>>'{world_ref,branch_id}',payload#>>'{scope,branch_id}')=:branch
GROUP BY 1,2,3,4 ORDER BY 1,2,3,4
"""


def snapshot(kernel, root, manifest, enrolled, out, stamp):
    books, windows, exports = {}, {}, []
    for book in BOOKS:
        project = root / book
        calls, events, generations, scopes_seen, sent, memories, signatures = (
            [],
            [],
            [],
            [],
            [],
            [],
            [],
        )
        waits = []
        extent = {
            "chapters": 0,
            "scenes": 0,
            "han": 0,
            "rolling_state": None,
            "target_chapters": None,
        }
        for phase, wr, owner in scopes(kernel, project, enrolled[book]):
            work, branch = wr["work_id"], wr["branch_id"]
            path = out / f"{stamp}-{book}-{phase}.jsonl"
            with private_export(path) as pending:
                meta = export_runtime_evidence(kernel, owner, work, branch, pending)
            exports.append(path)
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    row = json.loads(line)
                    if row["record"] == "call":
                        calls.append(row)
                    elif row["record"] == "event":
                        events.append(row)
                    elif row["record"] == "task_generation":
                        generations.append(row)
            scopes_seen.append({"phase": phase, **meta, "file": path.name})
            params = {"work": work, "branch": branch}
            with kernel.objects.lock(), kernel.db.uow(write=False, repeatable_read=True) as repo:
                kernel.authorize(repo, owner, work, "ADMIN", branch)
                sent.extend(
                    repo.conn.scalars(
                        text(_SENT),
                        {**params, **{k: fingerprint(v) for k, v in manifest["models"].items()}},
                    )
                )
                memories.extend(
                    dict(r) for r in repo.conn.execute(text(_MEMORY), params).mappings()
                )
                signatures.extend(
                    repo.conn.scalars(
                        text("""
                    SELECT payload->>'signature' FROM nf_artifacts WHERE work_id=:work
                    AND kind='P6_REFLECTION_RECORD' AND payload->>'branch_id'=:branch
                    AND payload->>'phase'='REPAIR_DECISION'
                """),
                        params,
                    )
                )
                queue = repo.conn.scalar(
                    text("""
                    SELECT extract(epoch FROM clock_timestamp()-requested_at)
                    FROM nf_dispatch_waiters WHERE branch_id=:branch AND valid_until>clock_timestamp()
                """),
                    params,
                )
                if queue is not None:
                    waits.append(float(queue))
                if phase == "writing":
                    extent = frozen_extent(kernel, repo, work, branch)
        current = {
            r["task_id"]: r for r in generations if r["current_generation"] == r["generation"]
        }
        observed = [r for r in calls if is_official(r)]
        measured = [
            r
            for r in observed
            if type(r["input_tokens"]) is int and type(r["output_tokens"]) is int
        ]
        spans = corroborated_spans(kernel.objects, observed)
        windows[book] = spans
        held = worker_active(project)
        live = (
            private(project / "worker-state.json")
            if (project / "worker-state.json").exists()
            else {}
        )
        books[book] = {
            "worker_lock_held": held,
            "worker_status": {k: live[k] for k in ("at", "status", "reason", "state") if k in live},
            "eligible_for_start": held and bool(sent or spans),
            "native_sent_running": sent,
            "scopes": scopes_seen,
            "runtime_versions": sorted(
                {
                    (r["role_id"], r["role_version"], r["skill"], r["skill_version"])
                    for r in generations
                    if r.get("skill_version")
                }
            ),
            "task_states": dict(Counter(r["current_state"] for r in current.values())),
            "active_tasks": [
                {
                    k: r[k]
                    for k in ("task_id", "generation", "task_kind", "current_state", "wait_mode")
                }
                for r in current.values()
                if r["current_state"] not in {"ACCEPTED", "CANCELLED", "STALE"}
            ],
            "attempt_ledger_states": dict(Counter(c["ledger_state"] for c in calls)),
            "completed_transport_count": len(spans),
            "audited_deepseek_attempts": len(observed),
            "measured_usage_calls": len(measured),
            "known_input_tokens": sum(r["input_tokens"] for r in measured),
            "known_output_tokens": sum(r["output_tokens"] for r in measured),
            "usage_complete": len(calls) == len(measured),
            "response_seconds": [(end - start).total_seconds() for _, start, end in spans],
            "usage_by_model": {
                model: {
                    "attempts": sum(c["model"] == model for c in observed),
                    "measured_usage_calls": sum(c["model"] == model for c in measured),
                    "known_output_tokens": sum(
                        c["output_tokens"] for c in measured if c["model"] == model
                    ),
                }
                for model in MODELS.values()
            },
            "observed_generation_retries": sum(r["generation"] > 0 for r in generations),
            "observed_call_reasons": dict(
                Counter(r["call_reason"] for r in observed if r["call_reason"])
            ),
            "event_types": dict(Counter(e["event_type"] for e in events if e["event_type"])),
            "error_codes": dict(Counter(e["error_code"] for e in events if e["error_code"])),
            "dispatch_waiter_age_seconds": waits,
            "upstream_provider_queue_seconds": None,
            "memory_records": memories,
            "repeated_repair_signatures": {k: n for k, n in Counter(signatures).items() if n > 1},
            "extent": extent,
        }
    with kernel.db.uow(write=False) as repo:
        activity = repo.conn.execute(
            text("""
            SELECT state, wait_event_type, count(*) AS connections FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY 1,2 ORDER BY 1,2
        """)
        ).mappings()
        db = [dict(row) for row in activity]
    return (
        {
            "captured_at": utc().isoformat(),
            "books": books,
            "database_activity_all_visible_connections": db,
        },
        windows,
        exports,
    )


def stage_estimate(samples, book, target):
    """Only project the measured checkpoint stage; external review/release has no cycle sample."""
    key = "chapters" if target["unit"] == "chapters" else "han"
    snapshots = [
        (datetime.fromisoformat(s["captured_at"]), s["books"][book]["extent"]) for s in samples
    ]
    rates = []
    for (a, old), (b, new) in pairwise(snapshots):
        if b > a and new[key] > old[key] and new["chapters"] > old["chapters"]:
            rates.append((new[key] - old[key]) / (b - a).total_seconds())
    current = snapshots[-1][1][key]
    result = {
        "unit": target["unit"],
        "target": target,
        "frozen_units": current,
        "fraction_of_minimum": min(current / target["min"], 1),
        "whole_book_eta": None,
        "whole_book_reason": "无法可靠估算整书完工时间",
        "positive_checkpoint_intervals": len(rates),
        "checkpoint_stage_estimate": None,
    }
    if len(rates) == len(MILESTONES) - 1 and snapshots[-1][1]["chapters"] >= 3:
        remaining = max(0, target["min"] - current)
        result["checkpoint_stage_estimate"] = {
            "target_units": target["min"],
            "scope": "to minimum frozen extent only; excludes final review/release",
            "optimistic_seconds": remaining / max(rates),
            "central_seconds": remaining / statistics.median(rates),
            "conservative_seconds": remaining / min(rates),
            "confidence": "low; three observed interval rates",
        }
    return result


def observe(kernel, root, output, price_card=None, start_wait=600):
    require(
        os.name == "posix",
        "INVALID_ARGUMENT",
        "Observe on the same POSIX host as the persistent workers",
    )
    manifest, enrolled = private(root / "batch.json"), private(root / "enrolled.json")
    require(
        set(enrolled) == set(BOOKS) and len({v["work_id"] for v in enrolled.values()}) == 3,
        "INVALID_EVIDENCE",
        "Three distinct registrations required",
    )
    require(
        manifest["database"] == kernel.db.engine.url.render_as_string(hide_password=True)
        and manifest["objects"] == str(kernel.objects.root),
        "INVALID_EVIDENCE",
        "Wrong installation",
    )
    require(
        set(manifest["models"]) == set(MODELS)
        and all(
            is_official_deepseek(v)
            and v["model"] == MODELS[k]
            for k, v in manifest["models"].items()
        ),
        "INVALID_EVIDENCE",
        "Wrong models",
    )
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    prepare_private_directory(output)
    deadline = time.monotonic() + start_wait
    while True:
        with TemporaryDirectory(dir=output) as tmp:
            check, _, _ = snapshot(kernel, root, manifest, enrolled, Path(tmp), "gate")
            if all(b["eligible_for_start"] for b in check["books"].values()):
                for export in Path(tmp).glob("*.jsonl"):
                    export.rename(output / export.name)
                save(output, "gate.json", check)
                break
        if time.monotonic() >= deadline:
            save(
                output,
                "not-started.json",
                {
                    "status": "NOT_STARTED",
                    "observed_at": utc().isoformat(),
                    "books": {
                        k: {
                            "worker_lock_held": b["worker_lock_held"],
                            "native_sent_running": b["native_sent_running"],
                            "completed_transport_count": b["completed_transport_count"],
                        }
                        for k, b in check["books"].items()
                    },
                },
            )
            return 3
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    clock, start = time.monotonic(), utc()
    samples = []
    for offset in MILESTONES:
        time.sleep(max(0, clock + offset - time.monotonic()))
        arrived = time.monotonic() - clock
        at = utc()
        sample, spans, paths = snapshot(kernel, root, manifest, enrolled, output, str(offset))
        sample.update(
            offset_target_seconds=offset,
            sampling_started_at=at.isoformat(),
            elapsed_seconds=time.monotonic() - clock,
            sampling_started_after_seconds=arrived,
        )
        save(output, f"sample-{offset}.json", sample)
        samples.append(sample)
    end = start + timedelta(seconds=MILESTONES[-1])
    transports = overlap(spans, start, end)
    proven = transports["all_three_overlap_proven"]
    summary = {
        "status": "OBSERVATION_COMPLETE",
        "parallel_execution": "THREE_BOOK_OVERLAP_VERIFIED" if proven else "THREE_BOOK_OVERLAP_UNPROVEN",
        "source_sha": manifest["source_sha"],
        "harness": manifest["harness"],
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "samples": [f"sample-{s}.json" for s in MILESTONES],
        "start_gate": "gate.json",
        "final_worker_lock_held": {
            k: b["worker_lock_held"] for k, b in samples[-1]["books"].items()
        },
        "captured_wall_seconds": (at - start).total_seconds(),
        "clock_skew_seconds": (at - start).total_seconds() - arrived,
        "usage_deltas_since_first_sample": {
            key: {
                field: samples[-1]["books"][key][field] - samples[0]["books"][key][field]
                for field in (
                    "completed_transport_count",
                    "audited_deepseek_attempts",
                    "measured_usage_calls",
                    "known_input_tokens",
                    "known_output_tokens",
                )
            }
            for key in BOOKS
        },
        "gate_evidence": {
            k: {
                "native_sent_running": b["native_sent_running"],
                "completed_transport_count": b["completed_transport_count"],
                "worker_lock_held": b["worker_lock_held"],
            }
            for k, b in check["books"].items()
        },
        "overlap": transports,
        "estimates": {},
        "price_estimates_cumulative": None,
        "price_status": "NO_DATED_VERIFIED_CARD",
    }
    for book, folder in BOOKS.items():
        idea = json.loads((ROOT / "examples/p6" / folder / "inspiration.json").read_bytes())
        require(
            fingerprint(idea) == manifest["ideas"][book],
            "SOURCE_CHANGED",
            "Inspiration revision differs",
        )
        summary["estimates"][book] = stage_estimate(samples, book, idea["length"])
    if price_card:
        priced = price_exports(paths, json.loads(price_card.read_bytes()))
        summary.update(
            price_estimates_cumulative=json.loads(json.dumps(priced["by_work"], default=str)),
            price_status="DATED_CARD_APPLIED_TO_TRANSPORT_UTC",
        )
    save(output, "summary.json", summary)
    return 0 if proven else 4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument(
        "--output", required=True, type=Path, help="new private directory on the worker host"
    )
    parser.add_argument("--price-card", type=Path)
    parser.add_argument("--start-wait-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.start_wait_seconds < 0:
        parser.error("Start wait must be nonnegative")
    db = Database(pool_size=2)
    try:
        kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
        return observe(
            kernel, args.private_dir, args.output, args.price_card, args.start_wait_seconds
        )
    except DomainError as exc:
        print(json.dumps({"status": "BLOCKED", "code": exc.code}))
        return 3
    except (OSError, ValueError, KeyError):
        print(json.dumps({"status": "BLOCKED", "code": "INVALID_OBSERVATION_INPUT_OR_OUTPUT"}))
        return 3
    except SQLAlchemyError:  # Database errors may include private connection settings.
        print(json.dumps({"status": "BLOCKED", "code": "OBSERVATION_SERVICE_UNAVAILABLE"}))
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
