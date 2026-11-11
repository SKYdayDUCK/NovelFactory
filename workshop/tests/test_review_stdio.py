"""Role-specific review intake and signing over independent authenticated MCP pipes."""
import asyncio
from contextlib import AsyncExitStack
from hashlib import sha256
import json
import os

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.review_admin import subject
from agent_workshop.reviews_skills import register_artifact
from test_reviews_skills import team
from test_w1_stdio import accepted, live_session


def test_independent_reviewer_signs_all_seven_modes_over_stdio(team, tmp_path, record_property):
    dsn, identity = team
    org, installation = identity("manager").org_id, identity("manager").installation_id
    bindings = {}
    with connect(dsn) as conn:
        subject(conn, org, "chief", "coauthor")
        for person in ("review-lead", "stats", "reviewer", "chief"):
            token = enroll(conn, installation, org, person, 1, 1)
            path = tmp_path / person
            with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
                json.dump({"database_url": dsn, "token": token}, output)
            bindings[person] = path
        for ref, producers in (
            ("direct", ["stats"]), ("coauthored", ["chief", "stats"]), ("proxied", ["writer", "chief"]),
        ):
            # stats and writer share the same verified NF subject in this team.
            register_artifact(conn, org, ref, "v1", sha256(ref.encode()).hexdigest(), "organization",
                              ref + "-receipt", producers, [ref + "-evidence"], {
                                  "STABILITY": {"requests": 4, "completed": 4, "duplicate_effects": 0},
                                  "STATISTICS": {"incremental_tokens": 8, "replayed_tokens": 8},
                                  "WORLD": {"opening": 3, "consumed": 1, "remaining": 2},
                              })

    modes = {
        "SCRIPT": ("MATERIAL_CHANGE", "CAUSALITY"), "DIRECTION": ("MATERIAL_CHANGE", "SCENE_ORDER"),
        "CHARACTER": ("DISCLOSURE", "MOTIVATION"), "PROSE": ("MATERIAL_CHANGE", "VOICE"),
        "WORLD": ("RETCON", "RESOURCE"), "STABILITY": ("INCIDENT", "RELIABILITY"),
        "STATISTICS": ("COST_DISPUTE", "ACCOUNTING"),
    }

    def request(ref, mode, risk):
        return {"operation_id": ref + "-" + mode, "artifact_ref": ref, "artifact_revision": "v1",
                "mode": mode, "risk_reason": risk, "ttl_seconds": 3600}

    async def denied(client, tool, args, code):
        reply = await client.call_tool(tool, args)
        assert reply.isError and reply.structuredContent == {"error": {"code": code}}

    async def run():
        async with AsyncExitStack() as stack:
            lead, alias, reviewer, coauthor = [
                await stack.enter_async_context(live_session(bindings[name]))
                for name in ("review-lead", "stats", "reviewer", "chief")
            ]
            for ref in ("direct", "coauthored", "proxied"):
                args = dict(request(ref, "PROSE", "MATERIAL_CHANGE"), operation_id="reject-" + ref,
                            reviewer_id="stats")
                await denied(lead, "aw_review_request", args, "INDEPENDENT_REVIEW_REQUIRED")
            for mode, (risk, reason) in modes.items():
                args = request("proxied", mode, risk)
                receipt = await accepted(lead, "aw_review_request", args)
                assert receipt["state"] == "PENDING" and receipt["execution"] == "HUMAN"
                assert await accepted(lead, "aw_review_request", args) == receipt
                ref = {"review_id": receipt["review_id"]}
                assigned = await accepted(reviewer, "aw_review_get", ref)
                assert (assigned["mode"], assigned["reviewer_id"], assigned["risk_reason"],
                        assigned["evidence_refs"]) == (mode, "reviewer", risk, ["proxied-evidence"])
                summary = await accepted(lead, "aw_review_get", ref)
                assert not {"artifact_ref", "evidence_refs", "content_sha"} & set(summary)
                await denied(alias, "aw_review_get", ref, "REVIEW_DENIED")
                signature = dict(ref, operation_id="sign-" + mode, outcome="APPROVED", reason=reason,
                                 evidence_ref="proxied-evidence")
                await denied(coauthor, "aw_review_decide", signature, "VIEW_DENIED")
                await denied(lead, "aw_review_decide", signature, "VIEW_DENIED")
                await denied(alias, "aw_review_decide", signature, "REVIEW_DENIED")
                await denied(reviewer, "aw_review_decide", dict(signature, evidence_ref="coauthored-evidence"),
                             "EVIDENCE_SCOPE_DENIED")
                signed = await accepted(reviewer, "aw_review_decide", signature)
                assert signed == dict(ref, state="APPROVED")
                assert await accepted(reviewer, "aw_review_decide", signature) == signed
            # Only trusted, routine sources can pass via deterministic arithmetic.
            routine = await accepted(lead, "aw_review_request", request("text", "STABILITY", "SAMPLED"))
            blocked = await accepted(lead, "aw_review_request", request("text", "WORLD", "SAMPLED"))
            assert (routine["state"], blocked["state"]) == ("RULE_PASSED", "RULE_BLOCKED")

    asyncio.run(run())
    with connect(dsn) as conn:
        assert conn.execute("SELECT count(*) AS n FROM aw_budget_holds WHERE org_id=%s", (org,)).fetchone()["n"] == 0
        rows = conn.execute("""SELECT mode,decision_reason FROM aw_reviews
                             WHERE org_id=%s AND state='APPROVED'""", (org,)).fetchall()
        assert {r["mode"]: r["decision_reason"] for r in rows} == {m: reason for m, (_, reason) in modes.items()}
    record_property("signed_modes", ",".join(modes))
    record_property("denied_reviewers", "original,coauthor,same NF subject,unassigned reviewer,lead")
