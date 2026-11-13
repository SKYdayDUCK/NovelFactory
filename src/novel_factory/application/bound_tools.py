"""Execute one tool against a live NF role, project or controller binding."""
import logging
from time import monotonic
from uuid import uuid4

from novel_factory.domain.errors import DomainError
from .audit_log import LogService
from .tool_catalog import _TOOLS, _VALIDATORS


_METHODS = {
    "nf_harness_metrics": "creative_metrics",
    "nf_harness_review_decide": "decide_creative_review",
    "nf_harness_review_independence": "issue_review_independence",
    "nf_harness_plan_propose": "propose_creative_plan",
    "nf_harness_simulate": "simulate_creative_plan",
    "nf_harness_plan_decide": "decide_creative_plan",
    "nf_rolling_window_submit": "submit_window",
    "nf_change_point_request": "request_change",
    "nf_change_point_review": "review_change",
    "nf_task_get": "task_get",
    "nf_context_get": "context_get",
    "nf_query_batch": "query_batch",
    "nf_artifact_put": "artifact_put",
}


def _dispatch(runtime, name, arguments):
    if name in {"nf_task_status", "nf_task_submit", "nf_task_cancel", "nf_task_events"}:
        return runtime.task_operation(name, arguments)
    if name in {"nf_project_status", "nf_resume_project"}:
        return runtime.project(resume=name == "nf_resume_project", **arguments)
    if name.startswith("nf_harness_instruction_"):
        return runtime.adaptive_instruction_call(name, **arguments)
    if name.startswith("nf_harness_agent_"):
        return runtime.dynamic_agent_call(name, **arguments)
    if name in {"nf_harness_review_read", "nf_harness_review_submit", "nf_harness_review_withdraw",
                "nf_harness_revision_brief", "nf_harness_revise"}:
        return runtime.creative_review_call(name, **arguments)
    return getattr(runtime, _METHODS[name])(**arguments)


def _evidence_id(name, result):
    if isinstance(result, dict):
        for key in ("artifact_ref", "change_point_ref", "window_ref", "review_ref",
                    "candidate_ref", "report_ref", "plan_ref"):
            ref = result.get(key)
            if isinstance(ref, dict) and ref.get("artifact_revision_id"):
                return ref["artifact_revision_id"]
        if name in {"nf_task_get", "nf_context_get"}:
            return result.get("context_bundle_id")
    return None


def invoke_bound_tool(runtime, name, arguments, *, ceiling=None, request_id=None, transport="local_tool"):
    """Apply tool ceilings, argument contract, business fences and audit to one call.

    The runtime and optional narrower ceiling are supplied by the trusted host.
    No database transaction spans discovery, remote checks or method dispatch.
    """
    if transport not in {"mcp", "local_tool"}:
        raise ValueError("Unsupported tool transport")
    started = monotonic()
    try:
        invocation, scope = LogService.begin_tool(
            runtime, name if name in _TOOLS else "UNREGISTERED",
            uuid4() if request_id is None else request_id, transport=transport,
        )
    except Exception:
        raise DomainError("SERVICE_UNAVAILABLE", "Tool audit is temporarily unavailable") from None
    error = None
    evidence = None
    try:
        if (ceiling is not None and name not in ceiling) or name not in runtime.available_tools():
            raise DomainError("VIEW_DENIED", "Tool unavailable for this binding")
        if not _VALIDATORS[name].is_valid(arguments):
            raise DomainError("INVALID_ARGUMENT", "Tool arguments are invalid")
        result = _dispatch(runtime, name, arguments)
        evidence = _evidence_id(name, result)
        return result
    except DomainError as exc:
        error = exc.code
        raise
    except Exception:
        error = "SERVICE_UNAVAILABLE"
        raise DomainError(error, "Tool is temporarily unavailable") from None
    finally:
        try:
            LogService.finish_tool(runtime, invocation, scope, error, round((monotonic() - started) * 1000, 3),
                                   evidence, transport=transport)
        except Exception:
            # A missing diagnostic cannot invalidate the authoritative business receipt.
            logging.getLogger(__name__).warning("Bound tool completion audit unavailable")
