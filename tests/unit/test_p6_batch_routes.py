"""New batches choose Codex/Flash while old inventory bindings remain readable."""
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_factory.domain.errors import DomainError


@pytest.fixture
def batch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("p6_batch")


def test_new_allowance_routes_review_to_codex_and_legacy_manifest_stays_unchanged(batch):
    allowance = batch.limits({"total_tokens": 1000000, "token_reservation": 500000,
                              "total_amount": "10", "reserve_amount": "1"})
    assert allowance["adapter_id"] == "deepseek_flash_writer"
    assert allowance["review_adapter_id"] == "codex_planner_reviewer"
    legacy = {"models": {"creative": {"model": "deepseek-flash"}, "review": {"model": "deepseek-v4-pro"}}}
    assert batch.batch_routes(legacy) == batch.LEGACY_ROUTES
    assert "routes" not in legacy
    assert batch.batch_routes({"routes": dict(batch.ROUTES)}) == batch.ROUTES
    with pytest.raises(DomainError):
        batch.batch_routes({"routes": {"review": "deepseek_pro_reviewer"}})


def test_preflight_only_checks_flash_catalog_and_codex_locally(batch, monkeypatch):
    checked, requested = [], []
    flash = {"model": "deepseek-flash", "isolation": "STATELESS_TEXT_ONLY", "adapter_kind": "deepseek",
             "execution_kind": "REAL_EXTERNAL", "provider": "https://api.deepseek.com",
             "endpoint": "https://api.deepseek.com/chat/completions"}
    codex = {"model": "gpt-6.1-sol", "isolation": "STATELESS_TEXT_ONLY", "adapter_kind": "codex-cli",
             "execution_kind": "REAL_HOST", "auth_mode": "host-login"}
    writer = SimpleNamespace(configuration=flash, profile=SimpleNamespace(preflight=lambda: "synthetic-key"))
    reviewer = SimpleNamespace(configuration=codex, check_available=lambda: checked.append("codex-host"))
    gateway = SimpleNamespace(adapters={batch.ROUTES["creative"]: writer, batch.ROUTES["review"]: reviewer})

    @contextmanager
    def response(request, timeout):
        requested.append(request.full_url)
        yield SimpleNamespace(read=lambda: b'{"data":[{"id":"deepseek-flash"}]}')

    monkeypatch.setattr(batch, "build_opener", lambda *_: SimpleNamespace(open=response))
    batch.check_models(gateway)
    assert checked == ["codex-host"]
    assert requested == ["https://api.deepseek.com/models"]
