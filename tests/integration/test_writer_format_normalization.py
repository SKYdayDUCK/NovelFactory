"""Complete synthetic HTTP bytes -> exact format proof -> ordinary candidate delivery."""
import base64
import hashlib
import json
import os
from copy import deepcopy

import pytest
from test_agent_gateway import setup_gateway
from test_p6_postpaid_delivery import fake_transport

from agent_runtime import providers
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.postpaid_delivery import require_postpaid_delivery
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.private_files import create_private_file

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def transport(kernel, monkeypatch, *, finish="stop", complete=True):
    payload = {"text": '工程正文 "ASCII引号" 和换行\n原样保留。', "event_refs": [],
               "important_deltas": ["仅工程声明"], "evidence_refs": []}
    adapter, calls = fake_transport(kernel, monkeypatch, "defer", lambda _packet: payload)
    original = providers.post
    bodies = []

    def post(**kwargs):
        body, receipt, window = original(**kwargs)
        http = json.loads(body)
        value = json.loads(http["choices"][0]["message"]["content"])
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        token = '"work_id":"' + value["world_ref"]["work_id"] + '"'
        raw = raw.replace(token, token[:-1])
        http["choices"][0]["message"]["content"] = raw
        http["choices"][0]["finish_reason"] = finish
        http["unrelated_provider_float"] = 0.125
        body = json.dumps(http).encode()
        target = kwargs["receipt_target"]
        record = {"version": "nf.http-receipt.v1", "attempt_id": target["attempt_id"],
            "request_sha256": kwargs["request_hash"], "body_sha256": hashlib.sha256(body).hexdigest(),
            "content": base64.b64encode(body).decode(), "complete": complete, "http_status": 200, "receipt": receipt}
        with os.fdopen(create_private_file(target["path"]), "wb") as out:
            out.write(json.dumps(record).encode())
        bodies.append(raw)
        return body, receipt, window

    monkeypatch.setattr(providers, "post", post)
    return adapter, calls, bodies, payload


def test_future_writer_quote_derivation_preserves_raw_and_unknown_invoice_and_replays(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    adapter, calls, bodies, payload = transport(kernel, monkeypatch)
    item = setup_gateway(kernel_env, adapter)
    item["request"]["call_key"] = "supervised-candidate"
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["result_status"] == "SUCCEEDED" and result["validation_status"] == "VALIDATED_CANDIDATE"
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": item["task"]["task_id"]})
        evidence, candidate = CandidateDelivery(kernel, item["gateway"]).audited_candidate(repo, task, production_accounting=True)
        assert candidate == payload and evidence["normalization_ref"]
        response = kernel.artifact(repo, evidence["response_ref"], task["work_id"])
        assert response["payload"]["text"] == bodies[0]
        with pytest.raises(json.JSONDecodeError):
            json.loads(response["payload"]["text"])
        saved = repo.one("task_results", {"result_id": task["current_result_id"]})
        assert saved["payload"]["artifacts"][-1] == evidence["normalization_ref"]
        proof = kernel.artifact(repo, evidence["normalization_ref"], task["work_id"])["payload"]
        assert proof["original_payload_slice_sha256"] == proof["normalized_payload_slice_sha256"]
        call = repo.one("call_attempts", {"attempt_id": evidence["attempt_id"]})
        assert call["state"] == "UNKNOWN" and call["actual_amount"] is None and call["actual_tokens"] is None
        audit = item["gateway"]._read_audit(repo, call["attempt_id"])
        original = kernel.artifact(repo, evidence["output_ref"], task["work_id"])
        altered = deepcopy(original)
        altered["payload"]["text"] = "被篡改的工程内容"
        with pytest.raises(DomainError):
            require_postpaid_delivery(kernel, repo, task, call, audit, response, altered)
    assert item["gateway"].execute(item["principal"], item["request"]) == result and len(calls) == 1


@pytest.mark.parametrize("finish,complete", [("length", True), ("stop", False)])
def test_incomplete_or_truncated_return_does_not_become_a_derived_candidate(kernel_env, monkeypatch, finish, complete):
    kernel = kernel_env["kernel"]
    adapter, calls, _, _ = transport(kernel, monkeypatch, finish=finish, complete=complete)
    item = setup_gateway(kernel_env, adapter)
    item["request"]["call_key"] = "supervised-candidate"
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["result_status"] == "FAILED"
    with kernel.db.uow(write=False) as repo:
        audit = item["gateway"]._read_audit(repo, result["attempt_id"])["payload"]
        assert not audit.get("normalization_ref") and audit["output_ref"] is None
    assert len(calls) == 1
