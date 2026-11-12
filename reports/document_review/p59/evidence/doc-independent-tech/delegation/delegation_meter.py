"""Collect duration/status of the registered delegation methods over SDK stdio."""
import functools
import json
import os
from pathlib import Path
import time
from mcp import ClientSession

samples=[]

def pytest_configure(config):
    original=ClientSession.call_tool
    @functools.wraps(original)
    async def call(session,name,*args,**kwargs):
        before=time.perf_counter_ns()
        response=await original(session,name,*args,**kwargs)
        content=response.structuredContent or {}
        samples.append({"method":name,"tool_error":bool(response.isError),"wall_ms":(time.perf_counter_ns()-before)/1e6,"task_state":content.get("task_state"),"accounting_state":content.get("accounting_state")})
        return response
    ClientSession.call_tool=call

def pytest_sessionfinish(session,exitstatus):
    Path(os.environ["DELEGATION_TIMING"]).write_text(json.dumps({"sha":"5e322e8af8ddb4465f68f8cab84c9685832461a9","path":"initialized stdio session; timing excludes host startup and list_tools","test_exit":int(exitstatus),"samples":samples},indent=2)+"\n")
