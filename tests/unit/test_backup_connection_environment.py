"""Recovery must reject routing ambiguity before opening either connection."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure import recovery
from novel_factory.infrastructure.online_backup import OnlineBackupService
from novel_factory.infrastructure.recovery import BackupService

pytestmark = pytest.mark.unit

URL = "postgresql+psycopg://nf_dev:fixture-password@127.0.0.1:56459/nf_generated"
ROUTING_VARIABLES = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")


@pytest.fixture(autouse=True)
def clean_routing_environment(monkeypatch):
    for key in ROUTING_VARIABLES:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("service_type", [BackupService, OnlineBackupService])
@pytest.mark.parametrize("variable", ROUTING_VARIABLES)
def test_ambient_route_fails_before_backup_connection_or_output(service_type, variable, monkeypatch, tmp_path):
    monkeypatch.setenv(variable, "fixture-routing-override")
    connect = Mock(side_effect=AssertionError("Connection must not be attempted"))
    service = service_type(SimpleNamespace(url=make_url(URL), connect=connect), None)
    destination = tmp_path / "never-created"
    with pytest.raises(DomainError) as denied:
        service.create(destination)
    assert denied.value.code == "BACKUP_TARGET_AMBIGUOUS"
    assert os.environ[variable] == "fixture-routing-override"
    assert not connect.called and not destination.exists()


@pytest.mark.parametrize("variable", ROUTING_VARIABLES)
def test_ambient_route_fails_before_restore_engine_or_object_write(variable, monkeypatch, tmp_path):
    monkeypatch.setenv(variable, "fixture-routing-override")
    engine_factory = Mock(side_effect=AssertionError("Restore engine must not be created"))
    monkeypatch.setattr(recovery, "create_engine", engine_factory)
    service = BackupService(None, None)
    verify = Mock(side_effect=AssertionError("Target rejection must precede backup reading"))
    monkeypatch.setattr(service, "verify", verify)
    objects = tmp_path / "never-created"
    with pytest.raises(DomainError) as denied:
        service.restore(tmp_path / "not-read", URL, objects)
    assert denied.value.code == "BACKUP_TARGET_AMBIGUOUS"
    assert not engine_factory.called and not verify.called and not objects.exists()


@pytest.mark.parametrize("query", [
    {"hostaddr": "127.0.0.2"},
    {"service": "another-cluster"},
    {"host": "127.0.0.2"},
    {"port": "6543"},
    {"dbname": "another-database"},
    {"sslmode": ("require", "disable")},
])
def test_unportable_url_options_fail_before_restore_engine(query, monkeypatch, tmp_path):
    engine_factory = Mock(side_effect=AssertionError("Unsupported URL must not be connected"))
    monkeypatch.setattr(recovery, "create_engine", engine_factory)
    target = make_url(URL).update_query_dict(query)
    with pytest.raises(DomainError) as denied:
        BackupService(None, None).restore(tmp_path / "not-read", target, tmp_path / "not-created")
    assert denied.value.code == "BACKUP_TARGET_AMBIGUOUS"
    assert not engine_factory.called


@pytest.mark.parametrize("changes", [
    {"host": "first-host,second-host"},
    {"host": "/var/run/postgresql"},
    {"host": ""},
    {"username": ""},
    {"database": ""},
    {"database": "host=127.0.0.2 dbname=other"},
    {"database": "postgresql://other/another-database"},
])
def test_target_requires_one_explicit_host_user_and_database(changes):
    with pytest.raises(DomainError) as denied:
        BackupService.validate_connection_target(make_url(URL).set(**changes))
    assert denied.value.code == "BACKUP_TARGET_AMBIGUOUS"


def test_missing_port_cannot_silently_use_ambient_pgport():
    with pytest.raises(DomainError) as denied:
        BackupService.validate_connection_target("postgresql+psycopg://nf_dev@127.0.0.1/nf_generated")
    assert denied.value.code == "BACKUP_TARGET_AMBIGUOUS"


def test_pg_tool_uses_exact_supported_url_without_editing_parent_environment(monkeypatch):
    for key in ROUTING_VARIABLES:
        monkeypatch.setenv(key, "")
    inherited = {"PGHOST": "different-host", "PGPORT": "6543", "PGUSER": "different-user",
                 "PGDATABASE": "different-database", "PGPASSWORD": "different-fixture-password",
                 "PGOPTIONS": "-c search_path=fixture_schema"}
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    url = make_url(URL).update_query_dict({"sslmode": "verify-full", "sslrootcert": "/fixture/ca.pem"})
    assert BackupService.validate_connection_target(url) == url
    child = BackupService._pg_env(url)
    assert child["PGHOST"] == "127.0.0.1" and child["PGPORT"] == "56459"
    assert child["PGUSER"] == "nf_dev" and child["PGDATABASE"] == "nf_generated"
    assert child["PGPASSWORD"] == "fixture-password"
    assert child["PGSSLMODE"] == "verify-full" and child["PGSSLROOTCERT"] == "/fixture/ca.pem"
    assert all(key not in child for key in (*ROUTING_VARIABLES, "PGOPTIONS"))
    assert {key: os.environ[key] for key in inherited} == inherited
    assert all(os.environ[key] == "" for key in ROUTING_VARIABLES)


@pytest.mark.skipif(os.name != "nt", reason="Windows PostgreSQL binaries use .exe")
def test_windows_backup_invokes_native_client_with_explicit_target(monkeypatch, tmp_path):
    executable = tmp_path / "pg_dump.exe"
    executable.write_bytes(b"fixture executable")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="dumped", stderr=""))
    monkeypatch.setattr(recovery.subprocess, "run", run)
    result = BackupService(None, None, pg_bin=tmp_path)._tool("pg_dump", ["--version"], URL)
    assert result["exit_code"] == 0
    args, options = run.call_args
    assert args[0] == [str(executable), "--version"]
    assert options["env"]["PGDATABASE"] == "nf_generated"
    assert options["env"]["PGPORT"] == "56459"
