"""Private PG17 startup is owner-bound, idempotent and shell-free."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("local_runtime_control", Path(__file__).parents[2] / "scripts/local_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def cluster(**changes):
    return {"owner": "nf", "data_directory": "/home/nf/private/pgdata", "port": 55434, **changes}


@pytest.mark.parametrize("running", [True, False])
def test_starts_private_cluster_as_owner_only_when_stopped(monkeypatch, running):
    calls = []
    def execute(command, **kwargs):
        assert "shell" not in kwargs
        calls.append(command)
        return SimpleNamespace(returncode=(0 if running else 3) if command[-1] == "status" else 0)
    monkeypatch.setattr(runtime.subprocess, "run", execute)
    runtime.start_services({"wsl_distribution": "NovelFactory", "postgres_clusters": [cluster()]})
    assert calls[:2] == [["wsl", "-d", "NovelFactory", "--", "service", service, "start"]
                         for service in ("postgresql", "redis-server")]
    assert calls[2] == ["wsl", "-d", "NovelFactory", "--user", "nf", "--",
                        "/usr/lib/postgresql/17/bin/pg_ctl", "-D", "/home/nf/private/pgdata", "status"]
    assert len(calls) == (3 if running else 4)
    if not running:
        assert calls[3][-3:] == ["-o", "-p 55434 -h 127.0.0.1", "start"]


@pytest.mark.parametrize("invalid", [cluster(owner="root;cmd"), cluster(port="55434"), cluster(port=22),
                                      cluster(data_directory="/etc/pgdata"),
                                      cluster(data_directory="/home/nf/../other"),
                                      cluster(data_directory="/home/nf")])
def test_invalid_cluster_is_rejected_before_any_service_mutation(monkeypatch, invalid):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("invalid config reached service"))
    with pytest.raises(ValueError, match="INVALID_PRIVATE_POSTGRES_CLUSTER"):
        runtime.start_services({"wsl_distribution": "NovelFactory", "postgres_clusters": [invalid]})


def test_pg_ctl_status_error_does_not_blind_start(monkeypatch):
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(runtime.subprocess, "run", execute)
    with pytest.raises(ValueError, match="PRIVATE_POSTGRES_STATUS_FAILED"):
        runtime.start_services({"wsl_distribution": "NovelFactory", "postgres_clusters": [cluster()]})
    assert calls[-1][-1] == "status"


def test_old_deployment_without_private_clusters_is_compatible(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: calls.append(command))
    runtime.start_services({"wsl_distribution": "NovelFactory"})
    assert len(calls) == 2


def test_test_redis_uses_disposable_database_separate_from_production(monkeypatch):
    config = {"databases": {}, "redis_password_ref": "REDIS_TEST_REF", "redis_port": 56379}
    monkeypatch.setattr(runtime, "read_private_bytes", lambda *args: json.dumps(config).encode())
    monkeypatch.setattr(runtime, "resolve", lambda _: "synthetic-only-password")
    _, env = runtime.environment(Path("synthetic-private-config"))
    assert env["NF_REDIS_URL"].endswith("/0") and env["AW_REDIS_URL"] == env["NF_REDIS_URL"]
    assert env["NF_TEST_REDIS_URL"].endswith("/15")
    assert env["NF_TEST_REDIS_URL"].rsplit("/", 1)[0] == env["NF_REDIS_URL"].rsplit("/", 1)[0]
