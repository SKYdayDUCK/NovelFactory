"""Pytest plugin: measure actual service methods inside reference scenarios."""
import functools
import importlib
import inspect
import json
import os
from pathlib import Path
import resource
import threading
import time
from sqlalchemy import event
from sqlalchemy.engine import Engine

METHODS = {
    "novel_factory.application.kernel.Kernel": ["create_task", "claim_task", "accept", "revision"],
    "novel_factory.application.queries.WorldQueryService": ["context_compile", "query"],
    "novel_factory.application.supervisor.RuntimeSupervisor": ["recover_project", "run_once", "admit_model"],
    "novel_factory.infrastructure.event_streams.Streams": ["read_batch"],
    "mcp.ClientSession":["call_tool"],
    "novel_factory.application.model_routing": ["select", "preview"],
    "novel_factory.infrastructure.model_gateway.OpenAICompatibleAdapter": ["generate"],
    "novel_factory.application.delegation.Delegations": ["call"],
    "novel_factory.application.production_pipeline": ["advance_pipeline"],
    "novel_factory.application.creative_review.CreativeReviewService": ["review", "decide"],
    "novel_factory.application.statistics.StatisticsService": ["read"],
}
samples=[]
stats=threading.local()
def before_cursor_execute(conn, cursor, statement, parameters, ctx, many):
    stats.count = getattr(stats,"count",0)+1

def instrument(owner, name, label):
    original = getattr(owner,name)
    def begin():
        return (time.monotonic_ns(), time.thread_time_ns(), getattr(stats,"count",0))
    def finish(start, err, args):
        wall,cpu,sql=start
        op = label
        if name=="call" and len(args)>1 and type(args[1]) is str:
            op+="."+args[1]
        if name=="query" and len(args)>2 and isinstance(args[2],dict):
            op+="."+args[2].get("query",{}).get("kind","other")
        samples.append(dict(operation=op, test=os.getenv("PYTEST_CURRENT_TEST","").split(" (")[0], wall_ms=(time.monotonic_ns()-wall)/1e6, thread_cpu_ms=(time.thread_time_ns()-cpu)/1e6, sql_cursor_calls=getattr(stats,"count",0)-sql, outcome=err, max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def wrapper(*args, **kw):
            start=begin()
            err="RETURNED"
            try:
                return await original(*args,**kw)
            except BaseException as exc:
                err=type(exc).__name__
                raise
            finally:
                finish(start,err,args)
    else:
        @functools.wraps(original)
        def wrapper(*args, **kw):
            start=begin()
            err="RETURNED"
            try:
                return original(*args,**kw)
            except BaseException as exc:
                err=type(exc).__name__
                raise
            finally:
                finish(start,err,args)
    setattr(owner,name,wrapper)

def pytest_configure(config):
    event.listen(Engine,"before_cursor_execute",before_cursor_execute)
    for owner_path,names in METHODS.items():
        mod,attribute=owner_path.rsplit(".",1)
        if attribute[0].islower():
            owner=importlib.import_module(owner_path)
        else:
            owner=getattr(importlib.import_module(mod),attribute)
        for name in names:
            instrument(owner,name,owner_path+"."+name)

def pytest_sessionfinish(session,exitstatus):
    Path(os.environ["P59_SAMPLES"]).write_text(json.dumps({"exit":int(exitstatus),"clock":"inclusive monotonic / current-thread CPU; nested measurements not additive; parent process only","samples":samples},ensure_ascii=False)+"\n")
