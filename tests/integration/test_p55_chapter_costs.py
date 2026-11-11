import json
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_agent_gateway import setup_gateway

from novel_factory.application.cost_reports import ChapterCostService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def sid():
    return str(uuid4())


def test_chapter_binding_scopes_rates_cache_and_missing_evidence(kernel_env, tmp_path):
    item = setup_gateway(kernel_env)
    world, kernel, gateway = item["world"], item["kernel"], item["gateway"]
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    scene, volume, chapter = sid(), sid(), sid()
    with kernel.db.uow() as repo:
        repo.conn.execute(
            text("INSERT INTO nf_trace_tasks(task_id,trace_id,volume_id) VALUES(:id,:s,:v)"),
            {"id": item["task"]["task_id"], "s": scene, "v": volume},
        )
        binding = gateway._binding(repo, item["principal"], item["request"])
    service = ChapterCostService(kernel)
    scope = (world["principal"], work, branch)
    base = {
        "schema_version": 1,
        "document_id": sid(),
        "version": 1,
        "work_id": work,
        "branch_id": branch,
    }
    mapping = {
        **base,
        "kind": "chapters",
        "chapters": [{"chapter_id": chapter, "volume_id": volume, "scene_ids": [scene, scene]}],
    }
    rates = {
        **base,
        "kind": "prices",
        "prices": [
            {
                "model": "selector",
                "provider": "fixture",
                "currency": "EUR",
                "input_per_million": "2",
                "cached_input_per_million": "1",
                "output_per_million": "4",
            },
            {
                "model": binding["config"]["model"],
                "currency": "CNY",
                "input_per_million": "1",
                "cached_input_per_million": "0.5",
                "output_per_million": "3",
            },
        ],
    }
    wrong_volume = deepcopy(mapping)
    wrong_volume["chapters"][0]["volume_id"] = sid()
    with pytest.raises(DomainError, match="Unavailable chapter scene or volume"):
        service.pin(*scope, wrong_volume)
    ch = service.pin(*scope, mapping)
    price = service.pin(*scope, rates)
    assert service.pin(*scope, rates) == price
    with pytest.raises(DomainError):
        service.pin(item["principal"], work, branch, rates)
    edited = deepcopy(rates)
    edited["prices"][0]["input_per_million"] = "3"
    with pytest.raises(DomainError, match="already pinned"):
        service.pin(*scope, edited)
    with pytest.raises(DomainError):
        service.pin(*scope, {**mapping, "schema_version": 2})
    collision = {
        **mapping,
        "document_id": sid(),
        "chapters": [*mapping["chapters"], {**mapping["chapters"][0], "chapter_id": sid()}],
    }
    with pytest.raises(DomainError):
        service.pin(*scope, collision)
    staged = service.report(*scope, ch, price)
    assert staged["chapters"][0]["estimated_amounts"] is None
    with pytest.raises(DomainError):
        service.report(*scope, ch, {**price, "sha256": "0" * 64})
    for bad in ("NaN", "1e99", "-1", 1.1):
        edited = deepcopy(rates)
        edited["prices"][0]["input_per_million"] = bad
        with pytest.raises(DomainError):
            service.pin(*scope, edited)
    selector_attempt = sid()
    payload = {
        "text": "private data",
        "input_tokens": 12,
        "output_tokens": 5,
        "cached_input_tokens": 2,
        "actual_amount": "0.02",
        "currency": "CNY",
        "cost_receipt_checked": True,
        "provider_receipt": "fixture",
        "model_configuration": {**binding["config"], "model": "selector"},
    }
    gateway._audit_artifact(
        binding, selector_attempt, "lookup-response", payload, "AGENT_ROLE_TOOL_RESPONSE"
    )
    gateway.execute(item["principal"], item["request"])
    report = service.report(*scope, ch, price)
    summary = report["chapters"][0]
    assert summary["scene_ids"] == [scene] and summary["observed_completed_calls"] == 2
    assert summary["reported_amounts"] == {"fixture": {"USD": "0.01", "CNY": "0.02"}}
    assert Decimal(summary["estimated_amounts"]["fixture"]["EUR"]) == Decimal("0.000042")
    assert Decimal(summary["estimated_amounts"]["fixture"]["CNY"]) == Decimal("0.000145")
    private = tmp_path / "operator.json"
    private.write_text(
        json.dumps(
            {
                "token": world["token"],
                "database_url": kernel_env["url"],
                "object_root": str(kernel_env["store"].root),
                "binding": {"work_id": work, "branch_id": branch},
            }
        )
    )
    private.chmod(0o600)
    rate_doc = tmp_path / "rates.json"
    rate_doc.write_text(json.dumps(rates))
    cli = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/p55_chapter_costs.py"),
        "--binding",
        str(private),
    ]
    pinned = json.loads(subprocess.check_output([*cli, "pin", str(rate_doc)], text=True))
    assert pinned == price
    chapter_path, price_path = tmp_path / "chapter.ref", tmp_path / "price.ref"
    chapter_path.write_text(json.dumps(ch))
    price_path.write_text(json.dumps(price))
    generated = json.loads(
        subprocess.check_output(
            [
                *cli,
                "report",
                str(chapter_path),
                "--price-ref",
                str(price_path),
                "--source-cut",
                report["source_cut"],
            ],
            text=True,
        )
    )
    assert generated == report
    assert report["price_book_version"] == 1 and report["invoiced_supplier_cost"] is None
    assert "private data" not in str(report) and len(summary["terminal_evidence"]) == 2
    assert (
        service.report(*scope, ch, price, source_cut=staged["source_cut"])["chapters"]
        == staged["chapters"]
    )
    no_prices = service.report(*scope, ch)["chapters"][0]
    assert (
        no_prices["reported_amounts"] == summary["reported_amounts"]
        and no_prices["estimated_amounts"] is None
    )
    v2 = {**rates, "version": 2, "prices": [rates["prices"][0]]}
    price2 = service.pin(*scope, v2)
    partial = service.report(*scope, ch, price2)["chapters"][0]
    assert partial["uncertainty"] == {"MISSING_RATE": 1} and partial["estimated_amounts"] is None
    assert "EUR" in partial["known_estimated_amounts"]["fixture"]
    fork = kernel.fork(world["principal"], world["world_ref"], "CANDIDATE", "cost-scope-test")
    fork_scope = (world["principal"], work, fork["branch_id"])
    with pytest.raises(DomainError):
        service.pin(*fork_scope, {**mapping, "branch_id": fork["branch_id"]})
    with pytest.raises(DomainError):
        service.report(*fork_scope, ch, price)
    other = load_fixture(kernel)
    with pytest.raises(DomainError):
        service.report(
            other["principal"],
            other["world_ref"]["work_id"],
            other["world_ref"]["branch_id"],
            ch,
            price,
        )


def test_missing_selector_cache_or_outcome_keeps_amount_unknown(kernel_env):
    item = setup_gateway(kernel_env)
    kernel, gateway, world = item["kernel"], item["gateway"], item["world"]
    work, branch = (world["world_ref"][k] for k in ("work_id", "branch_id"))
    scene, volume = sid(), sid()
    with kernel.db.uow() as repo:
        repo.conn.execute(
            text("INSERT INTO nf_trace_tasks(task_id,trace_id,volume_id) VALUES(:id,:s,:v)"),
            {"id": item["task"]["task_id"], "s": scene, "v": volume},
        )
        binding = gateway._binding(repo, item["principal"], item["request"])
    base = {
        "work_id": work,
        "branch_id": branch,
        "document_id": sid(),
        "version": 1,
        "schema_version": 1,
    }
    service = ChapterCostService(kernel)
    scope = (world["principal"], work, branch)
    ch = service.pin(
        *scope,
        {
            **base,
            "kind": "chapters",
            "chapters": [{"chapter_id": sid(), "scene_ids": [scene], "volume_id": volume}],
        },
    )
    p = service.pin(
        *scope,
        {
            **base,
            "kind": "prices",
            "prices": [
                {
                    "model": binding["config"]["model"],
                    "currency": "USD",
                    "input_per_million": "1",
                    "cached_input_per_million": "0.25",
                    "output_per_million": "2",
                }
            ],
        },
    )
    gateway._audit_artifact(
        binding, sid(), "lookup-input", {"messages": []}, "AGENT_ROLE_TOOL_PROMPT"
    )
    gateway._audit_artifact(
        binding,
        sid(),
        "lookup-response",
        {
            "text": "x",
            "model_configuration": binding["config"],
            "input_tokens": 10,
            "output_tokens": 5,
            "actual_amount": None,
            "currency": None,
            "provider_receipt": None,
            "cost_receipt_checked": False,
        },
        "AGENT_ROLE_TOOL_RESPONSE",
    )
    r = service.report(*scope, ch, p)["chapters"][0]
    assert r["reported_amounts"] is r["estimated_amounts"] is None
    assert r["uncertainty"] == {
        "PREPARED_WITHOUT_COMPLETION": 1,
        "MISSING_OR_INVALID_CACHE_USAGE": 1,
        "NO_CONFIRMED_GENERATION_AMOUNT": 1,
    }
