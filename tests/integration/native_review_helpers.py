"""Synthetic model callbacks may use this actual MCP path; never a live model helper."""
import asyncio
import json
from copy import deepcopy

from novel_factory.application.review_mcp import ReviewMcpBroker
from novel_factory.domain.canonical import canonical_bytes, fingerprint


def materialize_review_input(kernel, packet, receipt_refs):
    """Give a synthetic model only material actually returned by its own MCP calls.

    The original ModelRequest and saved creative snapshot remain ref-only. This
    local test view represents the completed tool conversation, not a larger
    production prompt or a background prefetch.
    """
    view = deepcopy(packet)
    materials = []
    with kernel.db.uow(write=False) as repo:
        for ref in receipt_refs:
            receipt_row = repo.one("artifacts", {"artifact_revision_id": ref["artifact_revision_id"]})
            receipt = kernel.artifact(repo, ref, receipt_row["work_id"], kind="NATIVE_REVIEW_TOOL_RECEIPT")["payload"]
            if receipt["tool"] != "nf_review_material" or receipt["outcome"] != "OK" or receipt["result"].get("isError"):
                continue
            source = receipt["arguments"]["artifact_ref"]
            value = receipt["result"]["structuredContent"]
            materials.append({"artifact_ref": source, "content": canonical_bytes(value).decode()})
    if not materials:
        return view
    by_ref = {fingerprint(item["artifact_ref"]): item for item in materials}
    # Preserve the source order frozen in the request; MCP helpers use that same order.
    view["authorized_materials"] = materials
    for item in view.get("structured_evidence", []):
        material = by_ref.get(fingerprint(item["ref"]))
        if material:
            item["value"] = json.loads(material["content"])
    if "creative" in view:
        prose = [value["text"] for item in materials if isinstance(value := json.loads(item["content"]), dict)
                 and isinstance(value.get("text"), str) and "draft_ref" in value]
        if prose:
            view["creative"]["prose"] = prose
    return view


def run_review_mcp(kernel, request, thread, turn, *, query_at=None, query_predicate=None):
    async def run():
        async with ReviewMcpBroker.from_attempt(kernel, request) as broker:
            broker.bind(thread, turn)
            await broker.call({"threadId": thread, "turnId": turn, "callId": "synthetic-context",
                               "tool": "nf_context_get", "arguments": {}})
            if "nf_query_batch" in broker.allowed_tools:
                with kernel.db.uow(write=False) as repo:
                    task_input = repo.one("task_inputs", {"task_id": broker.scope["task_id"], "generation": broker.scope["generation"]})
                    manifest = next(kernel.artifact(repo, ref, broker.scope["work_id"])["payload"]
                        for ref in task_input["spec"]["input_artifacts"]
                        if kernel.artifact(repo, ref, broker.scope["work_id"])["kind"] == "INPUT_MANIFEST")
                lookup = {"subject_id": manifest["subject_ids"][0], "predicate": manifest["predicates"][0]}
                if query_predicate is not None:
                    assert query_predicate in manifest["predicates"]
                    lookup["predicate"] = query_predicate
                if query_at is not None:
                    lookup["at"] = query_at
                await broker.call({"threadId": thread, "turnId": turn, "callId": "synthetic-query",
                    "tool": "nf_query_batch", "arguments": {"lookups": [lookup]}})
            for index, ref in enumerate(broker.scope["material_refs"]):
                await broker.call({"threadId": thread, "turnId": turn, "callId": "synthetic-material-" + str(index),
                    "tool": "nf_review_material", "arguments": {"artifact_ref": ref}})
            return list(broker.receipt_refs)
    return asyncio.run(run())
