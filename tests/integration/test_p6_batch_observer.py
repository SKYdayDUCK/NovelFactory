"""A registered fixture with a live lock cannot earn a paid production window."""

import importlib
import json
import os
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from agent_runtime._http_worker import _persist
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.native_files import file_lock
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.wire_receipts import destination


@pytest.fixture
def observer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("p6_observe_batch")


@pytest.mark.postgres
@pytest.mark.integration
def test_enrollment_and_live_workers_do_not_start_clock(kernel_env, tmp_path, observer):
    kernel = kernel_env["kernel"]
    root, output = tmp_path / "batch", tmp_path / "monitor"
    root.mkdir(mode=0o700)
    models = {
        k: {
            "model": v,
            "execution_kind": "REAL_EXTERNAL",
            "adapter_kind": "deepseek",
            "provider": "https://api.deepseek.com",
            "endpoint": "https://api.deepseek.com/chat/completions",
        }
        for k, v in observer.MODELS.items()
    }
    observer.save(
        root,
        "batch.json",
        {
            "database": kernel.db.engine.url.render_as_string(hide_password=True),
            "objects": str(kernel.objects.root),
            "models": models,
        },
    )
    enrolled = {}
    with ExitStack() as locks:
        for book in observer.BOOKS:
            work = load_fixture(kernel)
            project = root / book
            project.mkdir(mode=0o700)
            observer.save(
                project,
                "preparation.json",
                {"token": work["token"], "world_ref": work["world_ref"]},
            )
            enrolled[book] = work["world_ref"]
            locks.enter_context(file_lock(project / ".worker.lock"))
        observer.save(root, "enrolled.json", enrolled)
        assert observer.observe(kernel, root, output, start_wait=0) == 3
    status = json.loads((output / "not-started.json").read_bytes())
    assert all(
        v["worker_lock_held"] and v["completed_transport_count"] == 0
        for v in status["books"].values()
    )
    assert not (output / "sample-0.json").exists()
    assert not (output / "summary.json").exists()


@pytest.mark.unit
def test_transport_overlap_excludes_duplicates_boundaries_and_non_native(observer, tmp_path):
    t = datetime(2026, 1, 1, tzinfo=UTC)

    def span(a, b):
        return ("attempt", t + timedelta(seconds=a), t + timedelta(seconds=b))

    three = {"A": [span(-10, 4), span(1, 3)], "B": [span(2, 8)], "C": [span(3, 10)]}
    result = observer.overlap(three, t, t + timedelta(seconds=6))
    assert result["verified_three_book_seconds"] == 1
    three["C"] = [span(4, 6)]
    assert not observer.overlap(three, t, t + timedelta(seconds=6))["all_three_overlap_proven"]
    call = {
        "record": "call",
        "attempt_id": str(uuid4()),
        "request_hash": "b" * 64,
        "provider_receipt_sha256": sha256(b"fixture-receipt").hexdigest(),
        "call_origin": "PROVIDER",
        "official_deepseek_endpoint": True,
        "adapter": "deepseek",
        "provider": "api.deepseek.com",
        "model": "deepseek-flash",
        "execution_kind": "REAL_EXTERNAL",
        "sent_at": t.isoformat(),
        "received_at": (t + timedelta(seconds=4)).isoformat(),
    }
    store = ObjectStore(tmp_path / "objects")
    assert observer.corroborated_spans(store, [call]) == []
    opts = {"request_hash": call["request_hash"],
            "receipt_target": {"attempt_id": call["attempt_id"], "path": destination(store, call["attempt_id"])}}
    _persist(opts, 200, json.dumps({"model": call["model"]}).encode(), True, "fixture-receipt",
             {"started_at": call["sent_at"], "received_at": call["received_at"]})
    assert (
        len(
            observer.corroborated_spans(
                store,
                [
                    call,
                    {**call, "call_origin": "FIXTURE"},
                    {**call, "official_deepseek_endpoint": False},
                    {**call, "model": "deepseek-v4-pro"},
                    {**call, "sent_at": (t + timedelta(seconds=1)).isoformat()},
                    {**call, "received_at": None},
                    {**call, "execution_kind": "DETERMINISTIC_TEST"},
                ]
            )
        )
        == 1
    )


@pytest.mark.unit
def test_whole_book_eta_requires_more_than_raw_tokens(observer):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        {
            "captured_at": (start + timedelta(seconds=n)).isoformat(),
            "books": {"A": {"extent": {"chapters": i, "han": i * 100}}},
        }
        for i, n in enumerate(observer.MILESTONES)
    ]
    length = {"min": 6000, "max": 10000, "unit": "han_characters"}
    assert observer.stage_estimate(samples[:2], "A", length)["checkpoint_stage_estimate"] is None
    estimate = observer.stage_estimate(samples, "A", length)
    assert estimate["checkpoint_stage_estimate"]["target_units"] == 6000
    assert estimate["whole_book_eta"] is None


@pytest.mark.unit
@pytest.mark.parametrize("concurrent", [False, True])
def test_observer_reports_parallel_execution_separately_from_sampling(tmp_path, observer, monkeypatch, concurrent):
    """Virtual time tests scheduling only; no real book, transport, or observation is claimed."""
    from types import SimpleNamespace

    project = tmp_path / "batch"
    target = tmp_path / "snapshots"
    project.mkdir(mode=0o700)
    kernel = SimpleNamespace(
        db=SimpleNamespace(
            engine=SimpleNamespace(url=SimpleNamespace(render_as_string=lambda **kwargs: "db"))
        ),
        objects=SimpleNamespace(root=tmp_path),
    )
    models = {
        k: {
            "model": v,
            "adapter_kind": "deepseek",
            "execution_kind": "REAL_EXTERNAL",
            "provider": "https://api.deepseek.com",
            "endpoint": "https://api.deepseek.com/chat/completions",
        }
        for k, v in observer.MODELS.items()
    }
    observer.save(
        project,
        "batch.json",
        {
            "source_sha": "test",
            "models": models,
            "harness": {},
            "objects": str(tmp_path),
            "database": "db",
            "ideas": {
                k: observer.fingerprint(
                    json.loads(
                        (observer.ROOT / "examples/p6" / v / "inspiration.json").read_bytes()
                    )
                )
                for k, v in observer.BOOKS.items()
            },
        },
    )
    observer.save(project, "enrolled.json", {k: {"work_id": k} for k in observer.BOOKS})
    tick = [0.0]

    def sleep(duration):
        tick[0] += duration

    monkeypatch.setattr(observer, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: tick[0]))
    base = datetime(2026, 11, 13, tzinfo=UTC)
    monkeypatch.setattr(observer, "utc", lambda: base + timedelta(seconds=tick[0]))
    received = []

    def capture(kernel, root, manifest, enrolled, out, stamp):
        received.append((stamp, tick[0]))
        books = {
            k: {
                "extent": {"chapters": 0, "han": 0},
                "eligible_for_start": True,
                "worker_lock_held": True,
                "native_sent_running": ["dummy"],
                "completed_transport_count": 0,
                "audited_deepseek_attempts": 0,
                "measured_usage_calls": 0,
                "known_input_tokens": 0,
                "known_output_tokens": 0,
            }
            for k in observer.BOOKS
        }
        path = out / f"{stamp}-A-preparation.jsonl"
        path.write_text("instrumentation test")
        spans = {}
        for i, book in enumerate(books):
            offset = 10 if concurrent else 10 + i * 5
            spans[book] = ([(book, base + timedelta(seconds=offset), base + timedelta(seconds=offset + 5))]
                           if tick[0] >= offset + 5 else [])
        return (
            {"books": books, "captured_at": observer.utc().isoformat()},
            spans,
            [path],
        )

    monkeypatch.setattr(observer, "snapshot", capture)
    assert observer.observe(kernel, project, target, start_wait=0) == (0 if concurrent else 4)
    assert received == [("gate", 0), ("0", 0), ("120", 120), ("300", 300), ("600", 600)]
    assert (target / "gate-A-preparation.jsonl").is_file()
    summary = json.loads((target / "summary.json").read_bytes())
    assert summary["status"] == "OBSERVATION_COMPLETE"
    assert summary["captured_wall_seconds"] == 600
    assert summary["overlap"]["all_three_overlap_proven"] is concurrent
    assert summary["parallel_execution"] == (
        "THREE_BOOK_OVERLAP_VERIFIED" if concurrent else "THREE_BOOK_OVERLAP_UNPROVEN"
    )
