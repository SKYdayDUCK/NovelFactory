"""A paused P6 worker stays dormant until an authoritative scheduling token changes."""

import importlib
import signal
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_watch_does_not_repeat_unchanged_paused_work(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    script = importlib.import_module("p6_batch")
    calls, waits, logs, reads, closed = [], [], [], [], []
    stop = SimpleNamespace(stopped=False)
    stop.is_set = lambda: stop.stopped
    stop.wait = lambda seconds: None
    db = SimpleNamespace(engine="simulation-listener-engine", close=lambda: closed.append(True))
    kernel = SimpleNamespace(db=db, authenticate=lambda _: "simulated-principal")
    monkeypatch.setattr(script, "Database", lambda **kwargs: db)
    monkeypatch.setattr(script, "ObjectStore", lambda _: None)
    monkeypatch.setattr(script, "Kernel", lambda *args: kernel)
    monkeypatch.setattr(script, "Event", lambda: stop)
    monkeypatch.setattr(script, "AgentGateway", lambda *args: SimpleNamespace())
    monkeypatch.setattr(script, "load_runtime_profiles", lambda *args: {})
    monkeypatch.setattr(script, "configured_gateway", lambda *args: (_ for _ in ()).throw(AssertionError("Watch performed model preflight")))
    monkeypatch.setattr(script, "private", lambda _: {"work_id": "work", "branch_id": "branch", "token": "simulated"})
    monkeypatch.setattr(script, "prepare_private_directory", lambda _: None)
    monkeypatch.setattr(script, "file_lock", lambda _: nullcontext())
    monkeypatch.setattr(script, "wake_listener", lambda _: nullcontext("listener"))
    monkeypatch.setattr(script, "emit", lambda _project, result: logs.append(result))
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setenv("NF_OBJECT_ROOT", str(tmp_path / "unused-objects"))

    def token(*args):
        reads.append(True)
        return "unchanged" if len(reads) <= 3 else "authoritative-change"

    def serve(*args, shared_listener=None):
        assert shared_listener == "listener"
        calls.append(True)
        if len(calls) == 2:
            stop.stopped = True
        return 3

    monkeypatch.setattr(script, "project_change_token", token)
    monkeypatch.setattr(script, "serve", serve)
    monkeypatch.setattr(script, "wait_for_change", lambda listener, timeout: waits.append(timeout) or False)
    assert script.main(["worker", "--private-dir", str(tmp_path), "--book", "A", "--watch"]) == 3
    assert len(calls) == len(waits) == 2
    assert all(row["status"] == "WAITING_CHANGE" for row in logs)
    assert closed == [True]


def test_private_stop_request_checks_frozen_binding(monkeypatch, tmp_path):
    from threading import Event

    from novel_factory.domain.errors import DomainError

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    script = importlib.import_module("p6_batch")
    path = tmp_path / "stop.json"
    stop = script.StopRequest(Event(), path, "A", "frozen-source")
    assert not stop.is_set()
    path.write_text("simulation")
    monkeypatch.setattr(script, "private", lambda _: {"action": "STOP_AFTER_CURRENT_CALL", "book": "B", "source_sha": "frozen-source"})
    with pytest.raises(DomainError):
        stop.is_set()
    monkeypatch.setattr(script, "private", lambda _: {"action": "STOP_AFTER_CURRENT_CALL", "book": "A", "source_sha": "frozen-source"})
    assert stop.is_set()


@pytest.mark.parametrize("legacy", [False, True])
def test_stop_during_call_does_not_start_final_reader_handoff(monkeypatch, tmp_path, legacy):
    import sys
    from threading import Event

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    script = importlib.import_module("p6_batch")
    stop, logs = Event(), []
    project = tmp_path / "A"
    project.mkdir()
    (project / "first-window.json").touch()
    wr = {"work_id": "simulated-work", "branch_id": "simulated-branch"}
    models = {"creative": {"model": "simulated-flash"}, "review": {"model": "simulated-reviewer"}}
    manifest = {"source_sha": "frozen", "harness": {}, "models": models, "database": "frozen-db",
                "objects": str(tmp_path / "objects"), "allowances": {"A": {"writing": {}}}}
    routes = script.LEGACY_ROUTES if legacy else script.ROUTES
    if not legacy:
        manifest["routes"] = dict(routes)
    gateway = SimpleNamespace(creative_harness=SimpleNamespace(as_dict=dict),
                              adapters={routes[key]: SimpleNamespace(configuration=value) for key, value in models.items()})
    kernel = SimpleNamespace(objects=SimpleNamespace(root=tmp_path / "objects"),
                             db=SimpleNamespace(engine=SimpleNamespace(url=SimpleNamespace(render_as_string=lambda **kwargs: "frozen-db"))))
    documents = {"batch.json": manifest, "enrolled.json": {"A": wr}, "preparation.json": {"world_ref": wr}}
    monkeypatch.setattr(script, "private", lambda path: documents[path.name])
    monkeypatch.setattr(script, "source_release", lambda: "frozen")
    monkeypatch.setattr(script, "file_lock", lambda _: nullcontext())
    monkeypatch.setattr(script, "emit", lambda _project, result: logs.append(result))

    def completed_call(*args):
        stop.set()
        return {"status": "AWAITING_INDEPENDENT_REVIEW"}

    def forbidden_handoff(*args):
        raise AssertionError("Stop request started another paid reading")

    monkeypatch.setattr(script, "writing_step", completed_call)
    monkeypatch.setitem(sys.modules, "p6_gpt_handoff", SimpleNamespace(handle=forbidden_handoff))
    assert script.serve(kernel, gateway, tmp_path, "A", stop, shared_listener="simulation") == 0
    assert logs[-1] == {"status": "STOPPED", "reason": "OPERATOR_STOP_AFTER_CALL"}
