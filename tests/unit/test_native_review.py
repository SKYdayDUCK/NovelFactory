"""The native Host must request read tools; text that claims a read is not a call."""
import json
import sys

import pytest

from agent_runtime import native_codex, native_review, providers
from agent_runtime.host_process import start_host as real_start
from agent_runtime.protocol import ModelCallFailure, ModelRequest

pytestmark = pytest.mark.unit

STUB = r'''
import json,sys
def emit(value): print(json.dumps(value),flush=True)
def event(method,**fields):emit({'method':method,'params':{'threadId':'thread-fixture',**fields}})
def tool(index):
    name='nf_context_get' if index==1 else 'nf_review_material' if MODE=='blind' else 'nf_query_batch'
    emit({'id':100+index,'method':'item/tool/call','params':{
        'threadId':'different' if MODE=='wrong_thread' else 'thread-fixture','turnId':'turn-fixture',
        'callId':'call-'+str(index),'tool':'nf_artifact_put' if MODE=='write_tool' else name,'arguments':{}}})
def finish():
    event('thread/tokenUsage/updated',tokenUsage={'total':{'inputTokens':24,'outputTokens':14,'cachedInputTokens':3},
                                                'last':{'inputTokens':12,'outputTokens':7,'cachedInputTokens':0}})
    event('item/completed',turnId='turn-fixture',item={'id':'answer','type':'commandExecution' if MODE=='shell_item' else 'agentMessage',
                                                   'text':'{"claimed_queries":true}'})
    event('turn/completed',turn={'id':'turn-fixture','status':'completed'})
for line in sys.stdin:
    row=json.loads(line)
    if 'method' not in row:
        index=row['id']-100
        assert row['result']['success'] is (False if MODE=='tool_error' and index==1 else True)
        assert json.loads(row['result']['contentItems'][0]['text'])['receipt_ref']
        event('item/completed',turnId='turn-fixture',item={'type':'dynamicToolCall','id':'call-'+str(index),
            'tool':'nf_context_get' if index==1 else 'nf_review_material' if MODE=='blind' else 'nf_query_batch',
            'arguments':{},'status':'completed','success':row['result']['success']})
        if index==1:tool(2)
        else:finish()
        continue
    method=row['method'];params=row['params']
    if 'id' not in row:continue
    if method=='initialize':assert params['capabilities']['experimentalApi'];result={}
    elif method=='account/read':result={'account':{'type':'chatgpt'}}
    elif method=='thread/start':
        assert {x['name'] for x in params['dynamicTools']}==({'nf_context_get','nf_review_material'} if MODE in {'blind','blind_query'} else {'nf_context_get','nf_query_batch'})
        assert params['ephemeral'] and params['sandbox']=='read-only' and params['approvalPolicy']=='never'
        assert params['environments']==params['runtimeWorkspaceRoots']==params['selectedCapabilityRoots']==[]
        result={'thread':{'id':'thread-fixture'},'model':'fixture-review','modelProvider':'openai'}
    elif method=='mcpServerStatus/list':result={'data':[{'runtimeStatus':'ready'}] if MODE=='ambient_mcp' else []}
    elif method=='turn/start':result={'turn':{'id':'turn-fixture'}}
    else:raise RuntimeError(method)
    emit({'id':row['id'],'result':result})
    if method=='turn/start':
        if MODE=='self_claim':finish()
        else:tool(1)
'''


class BrokerFixture:
    def __init__(self):
        self.tool_specs = [{"type": "function", "name": name, "description": "Synthetic read", "inputSchema": {"type": "object"}}
                           for name in ("nf_context_get", "nf_query_batch")]
        self.receipt_refs, self.calls = [], []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def bind(self, thread, turn):
        self.bound = thread, turn
    async def call(self, params):
        assert (params["threadId"], params["turnId"]) == self.bound
        self.calls.append(params["tool"])
        ref = {"synthetic": params["callId"]}
        self.receipt_refs.append(ref)
        return {"success": not (getattr(self, "fail_first", False) and len(self.calls) == 1),
                "contentItems": [{"type": "inputText", "text": json.dumps({"result": {}, "receipt_ref": ref})}]}


def run_host(tmp_path, monkeypatch, mode="valid", authorize=lambda _: True):
    stub = tmp_path / "native_review_host.py"
    stub.write_text("MODE=" + repr(mode) + "\n" + STUB)
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", json.dumps({"codex-cli": ["fixture-review"]}))
    monkeypatch.setattr(native_review, "host_options", lambda **_kwargs: [])
    monkeypatch.setattr(native_review, "resolve_host_binary", lambda: sys.executable)
    async def start(*args, **kwargs):
        return await real_start(sys.executable, str(stub), **kwargs)
    monkeypatch.setattr(native_review, "start_host", start)
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic native review reached a live model")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    profile = providers.Profile("codex-cli", "fixture-review", "SYNTHETIC_NO_CREDENTIAL", 300,
                                auth_mode="host-login", model_output_ceiling=300)
    broker, records = BrokerFixture(), []
    broker.fail_first = mode == "tool_error"
    if mode in {"blind", "blind_query"}:
        broker.required_tools = {"nf_context_get", "nf_review_material"}
        broker.tool_specs = [{"type": "function", "name": name, "description": "Synthetic material read", "inputSchema": {"type": "object"}}
                             for name in broker.required_tools]
    response = native_review.generate(profile, ModelRequest(({"role": "user", "content": "Review"},), "a" * 64, 300, "attempt-fixture"),
        authorize=authorize, record=lambda *row: records.append(row), broker=broker)
    return response, broker, records


def test_native_dynamic_tools_are_real_requests_and_usage_covers_the_whole_turn(tmp_path, monkeypatch):
    response, broker, records = run_host(tmp_path, monkeypatch)
    assert broker.calls == ["nf_context_get", "nf_query_batch"]
    assert response.raw_response["review_mcp_receipt_refs"] == broker.receipt_refs
    assert (response.input_tokens, response.output_tokens, response.cached_input_tokens) == (24, 14, 3)
    assert [row[-1] for row in records] == ["THREAD_STARTED", "TURN_STARTED", "COMPLETED"]


def test_model_text_self_claim_cannot_invent_tool_receipts(tmp_path, monkeypatch):
    response, broker, _ = run_host(tmp_path, monkeypatch, "self_claim")
    assert json.loads(response.text)["claimed_queries"]
    assert response.raw_response["review_mcp_receipt_refs"] == broker.calls == []


def test_completed_host_tool_error_is_returned_without_inventing_success(tmp_path, monkeypatch):
    response, broker, _ = run_host(tmp_path, monkeypatch, "tool_error")
    assert broker.calls == ["nf_context_get", "nf_query_batch"]
    assert response.raw_response["review_mcp_receipt_refs"] == broker.receipt_refs


def test_blind_native_turn_only_offers_context_and_material_tools(tmp_path, monkeypatch):
    _, broker, _ = run_host(tmp_path, monkeypatch, "blind")
    assert broker.calls == ["nf_context_get", "nf_review_material"]


def test_blind_native_turn_rejects_a_world_query_even_from_host_rpc(tmp_path, monkeypatch):
    with pytest.raises(ModelCallFailure) as failure:
        run_host(tmp_path, monkeypatch, "blind_query")
    assert failure.value.code == "UNEXPECTED_HOST_TOOL_REQUEST"


@pytest.mark.parametrize("mode,code", [("wrong_thread", "UNEXPECTED_HOST_TOOL_REQUEST"),
    ("write_tool", "UNEXPECTED_HOST_TOOL_REQUEST"), ("shell_item", "UNEXPECTED_HOST_TOOL_ACTION"),
    ("ambient_mcp", "CODEX_HOST_TOOLS_ENABLED")])
def test_native_review_rejects_ambient_tools_and_cross_turn_requests(tmp_path, monkeypatch, mode, code):
    with pytest.raises(ModelCallFailure) as failure:
        run_host(tmp_path, monkeypatch, mode)
    assert failure.value.code == code
    assert failure.value.sent == (mode != "ambient_mcp")


def test_native_review_authorization_denial_remains_unsent(tmp_path, monkeypatch):
    with pytest.raises(ModelCallFailure) as failure:
        run_host(tmp_path, monkeypatch, authorize=lambda _: False)
    assert failure.value.code == "MODEL_CALL_NOT_AUTHORIZED" and not failure.value.sent


@pytest.mark.parametrize("approved,code", [(False, "MODEL_CALL_NOT_AUTHORIZED"), (True, "MODEL_NOT_ALLOWED")])
def test_independent_send_guard_survives_a_mocked_profile_preflight(monkeypatch, approved, code):
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1" if approved else "0")
    monkeypatch.setenv("NF_RUNTIME_ALLOWED_MODELS", '{"codex-cli":["another-model"]}')
    monkeypatch.setattr(native_review, "start_host", lambda *args, **kwargs: pytest.fail("Send guard reached a Host"))
    profile = providers.Profile("codex-cli", "fixture-review", "SYNTHETIC_NO_CREDENTIAL", 300,
                                auth_mode="host-login", model_output_ceiling=300)
    with pytest.raises(ModelCallFailure) as failure:
        native_review.generate(profile, ModelRequest(({"role": "user", "content": "Review"},), "a" * 64, 300, "attempt-fixture"),
            authorize=lambda _: True, record=lambda *args: None, broker=BrokerFixture())
    assert failure.value.code == code and not failure.value.sent


def test_only_private_review_bridge_enables_code_mode_and_keeps_ambient_tools_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[features]\nshell_tool=true\nmulti_agent_v2=true\n'
        '[mcp_servers.ambient]\ncommand="forbidden"\n', encoding="utf-8")

    def settings(options):
        assert len(options) % 2 == 0
        assert all(options[index] == "-c" for index in range(0, len(options), 2))
        return {key: json.loads(value) for key, value in (row.split("=", 1) for row in options[1::2])}

    text, review = settings(native_codex.host_options()), settings(native_review.review_host_options())
    assert text["features.code_mode"] is text["features.code_mode_host"] is False
    assert review["features.code_mode"] is review["features.code_mode_host"] is True
    assert {key: value for key, value in text.items() if key not in {"features.code_mode", "features.code_mode_host"}} == {
        key: value for key, value in review.items() if key not in {"features.code_mode", "features.code_mode_host"}}
    for name in ("shell_tool", "unified_exec", "apps", "browser_use", "computer_use", "multi_agent", "multi_agent_v2",
                 "view_image", "image_generation", "hooks", "plugins", "remote_plugin", "plugin_sharing",
                 "code_mode_only", "code_mode_tool_search"):
        assert review["features." + name] is False
    assert review["mcp_servers.ambient.enabled"] is False and review["web_search"] == "disabled"
    assert review["project_doc_max_bytes"] == 0
    assert native_review.REVIEW_TRANSPORT_REVISION == "task-bound-code-mode-v1"
