"""Check actual query receipts and exact current-text anchors before mandatory editing."""
import json

from ..domain.canonical import fingerprint
from ..domain.errors import DomainError
from ..domain.reconciliation import verify_span
from ..domain.review_gate import blocking_review_issues
from .kernel import require


def _known(value):
    if isinstance(value, dict):
        return value.get("value_status") in {"VALUE", "NONE"} or any(_known(v) for v in value.values())
    return isinstance(value, list) and any(_known(v) for v in value)


def require_review_evidence(kernel, repo, task, evidence, candidate):
    from .review_mcp import require_review_queries
    receipts = require_review_queries(kernel, repo, task, evidence)
    if "issues" not in candidate:
        return receipts
    frozen = kernel.artifact(repo, evidence["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
    drafts, texts, draft_texts = [], {}, {}
    for ref in frozen["material_refs"]:
        row = kernel.artifact(repo, ref, task["work_id"])
        if row["kind"] == "NARRATIVE_DRAFT":
            drafts.append(ref)
            text = row["payload"]["text_ref"]
            draft_texts[fingerprint(ref)] = fingerprint(text)
            texts[fingerprint(text)] = kernel.objects.read(text["sha256"]).decode("utf-8")
    response = kernel.artifact(repo, evidence["response_ref"], task["work_id"])["payload"]
    refs = json.loads(response["provider_response_json"])["review_mcp_receipt_refs"]
    queries = {fingerprint(ref): receipt for ref, receipt in zip(refs, receipts, strict=True)
               if receipt["tool"] == "nf_query_batch" and receipt["outcome"] == "OK"}
    for issue in blocking_review_issues(candidate["issues"]):
        require(issue["subject_ref"] in drafts and bool(issue["suggested_change"].strip()),
                "REVIEW_EVIDENCE_REQUIRED", "Mandatory advice must target its exact draft with a concrete correction")
        anchors = []
        query_proofs = []
        for source in issue["evidence_refs"]:
            if source["kind"] != "ARTIFACT":
                continue
            key = fingerprint(source["artifact_ref"])
            if key == draft_texts[fingerprint(issue["subject_ref"])] and source.get("span"):
                try:
                    verify_span(texts[key], source["span"])
                except DomainError:
                    require(False, "REVIEW_EVIDENCE_REQUIRED", "Review span does not match the current text")
                anchors.append((key, fingerprint(source["span"])))
            if key in queries:
                query_proofs.append(queries[key])
        require(anchors, "REVIEW_EVIDENCE_REQUIRED", "A mandatory correction needs a verified original-text span")
        if issue["category"] == "HARD_CONSISTENCY" or issue["issue_kind"] == "HARD_CONFLICT":
            require(query_proofs and (any(_known(q["result"]) for q in query_proofs)
                                     or len(set(anchors)) >= 2),
                    "REVIEW_UNPROVEN_CONFLICT", "Unknown state is not a proven contradiction; query internally first")
    return receipts
