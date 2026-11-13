"""The real supervisor loop parks blocked work and drains failed Futures."""

import importlib
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from novel_factory.infrastructure.agent_lifecycle import AgentLifecycleManager


def test_blocked_scheduler_waits_without_allocating_workers_or_busy_retry(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    script = importlib.import_module("run_supervisor")
    tick, calls, logs, stop = [0.0], [], [], Event()
    monkeypatch.setattr(script.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(script, "log_result", logs.append)

    def wake(_listener, timeout):
        tick[0] += timeout
        if tick[0] >= 10:
            stop.set()
        return False

    monkeypatch.setattr(script, "wait_for_change", wake)
    def run(*args, **kwargs):
        calls.append(tick[0])
        return {"status": "BLOCKED", "reason": "CALL_RECONCILIATION_REQUIRED"}

    with AgentLifecycleManager(lambda: (_ for _ in ()).throw(AssertionError("Pool created while blocked"))) as manager:
        script._drain(SimpleNamespace(run_once=run), None, None, None, manager, 2, {}, None, 5,
                      None, None, None, stop, manager)
        assert manager.starts == 0
    assert calls == [0.0, 5.0]


def test_worker_failure_is_reported_without_replaying_its_execution(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    script = importlib.import_module("run_supervisor")
    tick, calls, logs, stop = [0.0], [], [], Event()
    monkeypatch.setattr(script.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(script, "log_result", logs.append)

    class Pool:
        def submit(self, *_):
            calls.append("executed once")
            future = Future()
            future.set_exception(ValueError("injected worker error with private details"))
            return future

        def shutdown(self, wait=True):
            pass

    def run(*args, executor, **kwargs):
        return executor.submit(None) if not calls else {"status": "IDLE"}

    def wake(_listener, timeout):
        tick[0] += timeout
        if tick[0] >= 3:
            stop.set()
        return False

    monkeypatch.setattr(script, "wait_for_change", wake)
    with AgentLifecycleManager(Pool, idle_seconds=1, clock=lambda: tick[0]) as manager:
        script._drain(SimpleNamespace(run_once=run), None, None, None, manager, 1, {}, None, 5,
                      None, None, None, stop, manager)
        assert manager.stops == 1
    assert calls == ["executed once"]
    assert any(row.get("reason") == "WORKER_EXECUTION_FAILED" for row in logs)
    assert "private details" not in str(logs)
