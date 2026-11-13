"""Native login uses a fresh text-only thread and preserves the attempt send fence."""
import json
import sys

import pytest

from agent_runtime import native_codex, providers
from agent_runtime.host_process import start_host as real_start
from agent_runtime.protocol import ModelCallFailure, ModelRequest

pytestmark = pytest.mark.unit

STUB = '''
import json,sys
for line in sys.stdin:
    row=json.loads(line); method=row['method']; params=row['params']
    if 'id' not in row:continue
    if method=='initialize': result={}
    elif method=='account/read': result={'account':{'type':'chatgpt'}}
    elif method=='thread/start':
        assert params['ephemeral'] and params['sandbox']=='read-only' and params['approvalPolicy']=='never'
        result={'thread':{'id':'thread-fixture'},'model':'fixture-model','modelProvider':'openai'}
    elif method=='mcpServerStatus/list': result={'data':SERVERS,'nextCursor':None}
    elif method=='turn/start': result={'turn':{'id':'turn-fixture'}}
    else:raise RuntimeError(method)
    print(json.dumps({'id':row['id'],'result':result}),flush=True)
    if method=='turn/start':
        events=[('thread/tokenUsage/updated',{'tokenUsage':{'last':{'inputTokens':12,'outputTokens':7,'cachedInputTokens':3}}}),
                ('item/completed',{'turnId':'turn-fixture','item':{'type':ITEM,'text':'{"ok":true}'}}),
                ('turn/completed',{'turn':{'id':'turn-fixture','status':'completed'}})]
        for method,data in events:
            print(json.dumps({'method':method,'params':{'threadId':'thread-fixture',**data}}),flush=True)
'''


def run_stub(tmp_path, monkeypatch, *, servers=None, item="agentMessage", authorize=lambda _: True):
    stub = tmp_path / "host.py"
    stub.write_text("SERVERS=" + repr(servers or []) + "\nITEM=" + repr(item) + "\n" + STUB)
    monkeypatch.setenv("NF_RUNTIME_LIVE_APPROVED", "1")
    monkeypatch.setattr(providers, "resolve_host_binary", lambda: sys.executable)
    monkeypatch.setattr(native_codex, "resolve_host_binary", lambda: sys.executable)
    monkeypatch.setattr(native_codex, "host_options", list)

    async def host(*args, **kwargs):
        return await real_start(sys.executable, str(stub), **kwargs)

    monkeypatch.setattr(native_codex, "start_host", host)
    profile = providers.Profile(kind="codex-cli", model="fixture-model", secret_ref="NF_TEST_HOST",
        auth_mode="host-login", max_output_tokens=300, model_output_ceiling=300)
    request = ModelRequest(({"role": "user", "content": "Return JSON"},), "a" * 64, 300, "attempt-fixture")
    records = []
    result = providers.invoke(profile, request, authorize=authorize, record=lambda *row: records.append(row))
    return result, records


def test_login_thread_preserves_identity_usage_and_disabled_mcp_inventory(tmp_path, monkeypatch):
    response, records = run_stub(tmp_path, monkeypatch, servers=[{
        "name": "disabled-fixture", "runtimeStatus": "disabled", "tools": {}, "resources": [], "resourceTemplates": []}])
    assert json.loads(response.text) == {"ok": True}
    assert response.request_sha256 == "a" * 64
    assert (response.input_tokens, response.output_tokens, response.cached_input_tokens) == (12, 7, 3)
    assert response.actual_amount is None
    assert [row[-1] for row in records] == ["THREAD_STARTED", "TURN_STARTED", "COMPLETED"]


def test_authorization_denial_is_proven_unsent(tmp_path, monkeypatch):
    with pytest.raises(ModelCallFailure) as denied:
        run_stub(tmp_path, monkeypatch, authorize=lambda _: False)
    assert denied.value.code == "MODEL_CALL_NOT_AUTHORIZED" and not denied.value.sent


def test_active_mcp_is_rejected_before_model_turn(tmp_path, monkeypatch):
    with pytest.raises(ModelCallFailure) as denied:
        run_stub(tmp_path, monkeypatch, servers=[{"name": "fixture", "runtimeStatus": "ready", "tools": {}}])
    assert denied.value.code == "CODEX_HOST_TOOLS_ENABLED" and not denied.value.sent


def test_unexpected_host_action_cannot_become_an_accepted_text_result(tmp_path, monkeypatch):
    with pytest.raises(ModelCallFailure, match="UNEXPECTED_HOST_TOOL_ACTION") as denied:
        run_stub(tmp_path, monkeypatch, item="commandExecution")
    assert denied.value.sent
