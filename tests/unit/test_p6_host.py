"""Durable host control must not turn health/restart/stop into paid retries."""
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("p6_host", Path(__file__).parents[2] / "scripts/p6_host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


@pytest.fixture
def config(tmp_path, monkeypatch):
    state = tmp_path / "private"
    (state / "batch/A").mkdir(parents=True)
    (state / "batch/batch.json").write_text(json.dumps({"source_sha": "frozen", "database": "unused"}))
    value = {"version": 1, "source": str(tmp_path / "source"), "state": str(state),
             "deployment": str(tmp_path / "deployment.json"), "books": ["A"]}
    path = state / "host.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(host, "read_private_bytes", lambda path, limit: Path(path).read_bytes())
    monkeypatch.setattr(host.subprocess, "check_output", lambda *a, **k: "frozen\n")
    return path, host.configuration(path)


def test_plan_is_offline_and_does_not_resolve_credentials(config, monkeypatch, capsys):
    path, _ = config
    def forbidden(*args, **kwargs):
        pytest.fail("offline host plan must not touch credentials, services or a provider")
    monkeypatch.setattr(host, "resolve", forbidden)
    monkeypatch.setattr(host, "database_health", forbidden)
    monkeypatch.setattr(host.subprocess, "run", forbidden)
    monkeypatch.setattr(host.subprocess, "Popen", forbidden)
    monkeypatch.setattr(host.sys, "argv", ["p6_host", "--config", str(path), "plan"])
    assert host.main() == 0
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0


def test_scheduler_uses_current_user_unlimited_duration_and_bounded_restart(tmp_path):
    xml = ET.fromstring(host.task_xml(tmp_path / 'private & data/host.json', "B", "S-1-5-21-example"))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    def read(path):
        return xml.find(path, ns).text
    assert read("t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert read("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert read("t:Settings/t:AllowHardTerminate") == "false"
    assert read("t:Settings/t:RestartOnFailure/t:Count") == "3"
    assert read("t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert "--book B" in read("t:Actions/t:Exec/t:Arguments")
    assert "private & data" in read("t:Actions/t:Exec/t:Arguments")
    assert "password" not in ET.tostring(xml, encoding="unicode").lower()


def test_rejects_source_transition_without_manifest_update(config, monkeypatch):
    path, _ = config
    monkeypatch.setattr(host.subprocess, "check_output", lambda *a, **k: "different\n")
    with pytest.raises(ValueError, match="SOURCE_BINDING"):
        host.configuration(path)


def test_stop_is_bound_after_current_call_and_cannot_escape_private_state(config, monkeypatch):
    _, value = config
    active = value["state"] / "A-active-worker.json"
    stop = value["state"] / "A.stop.json"
    active.write_text(json.dumps({"source_sha": "frozen", "stop_file": str(stop)}))
    saved = []
    monkeypatch.setattr(host, "save", lambda path, body: saved.append((path, body)))
    host.request_stop(value, ["A"])
    assert saved == [(stop, {"action": "STOP_AFTER_CURRENT_CALL", "book": "A", "source_sha": "frozen"})]
    active.write_text(json.dumps({"source_sha": "old", "stop_file": str(stop)}))
    with pytest.raises(ValueError, match="SOURCE_BINDING"):
        host.request_stop(value, ["A"])
    active.write_text(json.dumps({"source_sha": "frozen", "stop_file": str(value["state"].parent / "escape")}))
    with pytest.raises(ValueError, match="STOP_PATH"):
        host.request_stop(value, ["A"])
    assert len(saved) == 1


def test_status_uses_kernel_locks_instead_of_reused_pid(config, monkeypatch):
    _, value = config
    monkeypatch.setattr(host, "database_health", lambda _: {"database": "READY", "redis": "READY"})
    monkeypatch.setattr(host, "lock_is_held", lambda path: path.name == ".manager.lock")
    assert host.status(value)["books"]["A"] == {"host": False, "manager": True, "worker": False}


def test_existing_manager_does_not_launch_another_worker(config, monkeypatch):
    _, value = config
    monkeypatch.setattr(host, "lock_is_held", lambda path: path.name == ".manager.lock")
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate dispatch"))
    with pytest.raises(ValueError, match="ALREADY_RUNNING"):
        host.run(value, "A")


def test_infrastructure_failure_cannot_reach_dispatch(config, monkeypatch):
    import sys
    from contextlib import nullcontext

    _, value = config
    monkeypatch.setattr(host, "lock_is_held", lambda _: False)
    monkeypatch.setattr(host, "file_lock", lambda _: nullcontext())
    monkeypatch.setattr(host, "awake", nullcontext)
    monkeypatch.setitem(sys.modules, "local_runtime", SimpleNamespace(environment=lambda _: ({}, {})))
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(host, "database_health", lambda _: {"database": "UNAVAILABLE", "redis": "READY"})
    monkeypatch.setattr(host.subprocess, "Popen", lambda *a, **k: pytest.fail("unhealthy dispatch"))
    with pytest.raises(ValueError, match="INFRASTRUCTURE_NOT_READY"):
        host.run(value, "A")


def test_host_propagates_failure_without_retrying_or_killing_paid_child(config, monkeypatch):
    import os
    import sys
    from contextlib import nullcontext

    _, value = config
    monkeypatch.setattr(host, "lock_is_held", lambda _: False)
    monkeypatch.setattr(host, "file_lock", lambda _: nullcontext())
    monkeypatch.setattr(host, "awake", nullcontext)
    monkeypatch.setitem(sys.modules, "local_runtime", SimpleNamespace(environment=lambda _: ({}, {})))
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(host, "database_health", lambda _: {"database": "READY", "redis": "READY"})
    monkeypatch.setattr(host, "create_private_file", lambda path: os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    monkeypatch.setattr(host, "open_append", lambda path: path.open("ab"))
    launched, saved = [], []
    def launch(command, **kwargs):
        launched.append(command)
        return SimpleNamespace(pid=123, wait=lambda: 3)
    monkeypatch.setattr(host.subprocess, "Popen", launch)
    monkeypatch.setattr(host, "save", lambda path, body: saved.append(body))
    assert host.run(value, "A") == 3
    assert len(launched) == len(saved) == 1
    assert "--watch" in launched[0] and "--stop-file" in launched[0]
    assert saved[0]["source_sha"] == "frozen"
    assert saved[0]["pid"] == 123
