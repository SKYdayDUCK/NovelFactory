"""Opt-in stdio experiment: coalesce only readers already inside the bound tool gate."""
from collections import Counter
from concurrent.futures import Future
from copy import deepcopy
from threading import Condition
from time import monotonic

from novel_factory.application.dynamic_runtime import assignment_from_inputs, matching_inputs
from novel_factory.application.mcp_runtime import TaskBinding
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces import mcp_stdio


class StaticReadGroup:
    def __init__(self, runtime, window=.001):
        self.runtime = runtime
        self.original = {"nf_task_get": runtime.task_get, "nf_context_get": runtime.context_get}
        self.condition = Condition()
        self.active = None
        self.window = window
        self.shapes = Counter()

    def read(self, name):
        result = Future()
        with self.condition:
            group = self.active
            leader = group is None
            if leader:
                group = self.active = []
            group.append((name, result))
            if len(group) == 2:
                self.active = None
                self.condition.notify_all()
            if leader:
                deadline = monotonic() + self.window
                self.condition.wait_for(lambda: len(group) == 2, timeout=max(0, deadline-monotonic()))
                if self.active is group:
                    self.active = None
                self.shapes[len(group)] += 1
        if leader:
            self._execute(group)
        # A follower never waits inside a DB transaction. Admission to the
        # existing 2-slot pool bounds the number of readers retaining a slot.
        return result.result()

    def _execute(self, group):
        try:
            if len(group) > 1:
                runtime = self.runtime
                with runtime.kernel.db.uow(write=False, repeatable_read=True) as repo:
                    state = runtime._bound_state(repo)
                    task, _, context, spec = state
                    inputs = matching_inputs(repo, task, spec)
                    if assignment_from_inputs(inputs) is None:
                        names = runtime._task_tools(repo, state, inputs)
                        summary = {key: task[key] for key in ("task_id", "generation", "state", "context_bundle_id")}
                        summary["world_ref"] = context["payload"]["world_ref"]
                        values = {"nf_task_get": summary, "nf_context_get": context["payload"]}
                        outcomes = [deepcopy(values[name]) if name in names else None for name, _ in group]
                    else:
                        outcomes = None
                # Publish only after shared fresh transaction has exited.
                if outcomes is not None:
                    for (_, future), value in zip(group, outcomes, strict=True):
                        if value is None:
                            future.set_exception(DomainError("VIEW_DENIED", "Tool outside task authority"))
                        else:
                            future.set_result(value)
                    return
            for name, future in group:
                try:
                    future.set_result(self.original[name]())
                except Exception as exc:
                    future.set_exception(exc)
        except Exception as exc:
            for _, future in group:
                if not future.done():
                    code = exc.code if isinstance(exc, DomainError) else "SERVICE_UNAVAILABLE"
                    future.set_exception(DomainError(code, "Grouped read unavailable"))


def install():
    factory = mcp_stdio.runtime_from_config
    collectors = []

    def bound_runtime(*args, **kwargs):
        runtime = factory(*args, **kwargs)
        if isinstance(runtime.binding, TaskBinding):
            reader = StaticReadGroup(runtime)
            collectors.append(reader)
            runtime.task_get = lambda: reader.read("nf_task_get")
            runtime.context_get = lambda: reader.read("nf_context_get")
        return runtime

    mcp_stdio.runtime_from_config = bound_runtime
    return collectors
