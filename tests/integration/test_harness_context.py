"""S1 exercises the real gateway's task, dispatch, snapshot, and delivery paths."""
from dataclasses import replace
import json

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from test_agent_gateway import FixtureAdapter, setup_gateway


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


class EnvelopeFixture(FixtureAdapter):
    """Use the gateway's output identity to emit one deterministic author candidate."""
    def generate(self, request):
        system = request.messages[-1]["content"]
        identity = json.loads(system.split("\nIdentity: ", 1)[1].split("\n", 1)[0])
        static_input = {"role": "user", "content": json.dumps({"context": identity})}
        result = super().generate(replace(request, messages=(request.messages[0], static_input)))
        self.calls[-1] = request
        return result


def configured(monkeypatch, active=True):
    config = CreativeHarnessConfig(enabled=active)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(config.as_dict()))


def test_frozen_model_input_survives_restart_with_feature_off(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    configured(monkeypatch, False)
    gateway = AgentGateway(item["kernel"], {"fixture": item["adapter"]})
    request = item["request"]
    receipt = gateway.execute(item["principal"], request)
    assert receipt["validation_status"] == "VALIDATED_CANDIDATE"
    user = json.loads(item["adapter"].calls[0].messages[1]["content"])
    assert "creative_materials" in user and "read_cut" not in user
    assert "dependency_fingerprint" not in json.dumps(user["creative_materials"])
    with item["kernel"].db.uow(write=False) as repo:
        records = repo.many("artifacts", {"work_id":item["world"]["world_ref"]["work_id"],
                                         "kind":"CREATIVE_CONTEXT_SNAPSHOT"})
        assert len(records) == 1
        snap = records[0]["payload"]
        packet = validate_contract("ContextPacket", snap["packet"], "harness")
        assert snap["model_input"] == user
        assert fingerprint(snap) == records[0]["sha256"]
        assert packet["scope"]["task_id"] == request["task_id"]
        assert packet["scope"]["generation"] == request["generation"]
        assert packet["context"]["world_ref"] == item["world"]["world_ref"]
        assert any("No assigned style" in reason for reason in packet["omissions"])
        assert "style" not in user["creative_materials"] and "voices" not in user["creative_materials"]
    assert gateway.execute(item["principal"], request) == receipt
    assert len(item["adapter"].calls) == 1


def test_existing_static_task_survives_new_enabled_gateway(kernel_env, monkeypatch):
    configured(monkeypatch, False)
    item = setup_gateway(kernel_env)
    configured(monkeypatch, True)
    gateway = AgentGateway(item["kernel"], {"fixture": item["adapter"]})
    result = gateway.execute(item["principal"], item["request"])
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    user = json.loads(item["adapter"].calls[0].messages[1]["content"])
    assert "read_cut" in user and "creative_materials" not in user
    with item["kernel"].db.uow(write=False) as repo:
        assert not repo.many("artifacts", {"work_id":item["world"]["world_ref"]["work_id"],
                                          "kind":"CREATIVE_CONTEXT_SNAPSHOT"})


def test_revocation_during_external_call_prevents_harness_delivery(kernel_env, monkeypatch):
    configured(monkeypatch)
    adapter = EnvelopeFixture()
    item = setup_gateway(kernel_env, adapter)
    def revoke_grant(_):
        item["kernel"].revoke_grant(item["world"]["principal"], item["principal"].grant_id)
    adapter.hook = revoke_grant
    with pytest.raises(DomainError) as error:
        item["gateway"].execute(item["principal"], item["request"])
    assert error.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as replay:
        item["gateway"].execute(item["principal"], item["request"])
    assert replay.value.code == "VIEW_DENIED"
    assert len(adapter.calls) == 1
    with item["kernel"].db.uow(write=False) as repo:
        assert repo.many("task_results", {"task_id": item["request"]["task_id"]}) == []


def test_restart_after_freeze_reuses_same_bytes_without_reading_materials_again(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    gateway, req, principal = item["gateway"], item["request"], item["principal"]
    with item["kernel"].db.uow(write=False) as repo:
        initial = gateway._binding(repo, principal, req)
    before = gateway._messages(principal, initial)
    snapshot_ref = initial["creative_snapshot"][0]
    resumed = AgentGateway(item["kernel"], {"fixture": item["adapter"]})
    def unexpected(*args):
        pytest.fail("A resumed task re-read sources instead of using its frozen packet")
    monkeypatch.setattr(resumed, "_authorized_materials", unexpected)
    receipt = resumed.execute(principal, req)
    assert receipt["validation_status"] == "VALIDATED_CANDIDATE"
    assert item["adapter"].calls[0].messages == before
    with item["kernel"].db.uow(write=False) as repo:
        work = item["world"]["world_ref"]["work_id"]
        audit = item["kernel"].artifact(repo, receipt["evidence_ref"], work)["payload"]
        assert audit["creative_context_ref"] == snapshot_ref
