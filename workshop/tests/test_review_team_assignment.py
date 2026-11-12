import asyncio
from contextlib import AsyncExitStack
import json
import os

import pytest

from agent_workshop import reviews_skills as reviews
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import TOOLS
from agent_workshop.review_admin import subject
from test_reviews_skills import team
from test_w1_stdio import accepted, live_session


def test_automatic_and_manual_assignment_follow_the_review_leads_team(team):
    dsn, identity = team
    manager = identity("manager")
    with connect(dsn) as conn:
        hire = TOOLS["aw_hire"][2]
        hire(conn, manager, expected_revision=1, operation_id="hire-lead",
             employee_id="second-lead", role_id="review_lead", supervisor_id="manager", team_id="audit-2")
        hire(conn, manager, expected_revision=2, operation_id="hire-specialist",
             employee_id="second-reviewer", role_id="reviewer", supervisor_id="second-lead", team_id="audit-2")
        subject(conn, manager.org_id, "second-reviewer", "independent-second")
        token = enroll(conn, manager.installation_id, manager.org_id, "second-lead", 1, 1)
        lead = authenticate(conn, token)
        review = reviews.review_request(conn, lead, "auto", "text", "v1", "PROSE", "SAMPLED", 60)
        assert reviews.get_review(conn, lead, review["review_id"])["reviewer_id"] == "second-reviewer"
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW_REQUIRED"):
            reviews.review_request(conn, lead, "manual", "text", "v1", "DIRECTION", "SAMPLED",
                                   60, reviewer_id="reviewer")
        direct = reviews.review_request(conn, identity("review-lead"), "direct", "text", "v1",
                                        "DIRECTION", "SAMPLED", 60, reviewer_id="reviewer")
        assert reviews.get_review(conn, identity("review-lead"), direct["review_id"])["reviewer_id"] == "reviewer"

        # Suspending the original reviewer preserves which team owned that tenure.
        TOOLS["aw_suspend"][2](conn, manager, expected_revision=3, operation_id="suspend",
                               employee_id="second-reviewer")
        hire(conn, manager, expected_revision=4, operation_id="hire-relief",
             employee_id="relief", role_id="reviewer", supervisor_id="second-lead", team_id="audit-2")
        subject(conn, manager.org_id, "relief", "independent-relief")
        with pytest.raises(Denied, match="REVIEW_DENIED"):
            reviews.reconcile_review(conn, identity("review-lead"), "unrelated-team",
                                     review["review_id"], "reviewer")
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW_REQUIRED"):
            reviews.reconcile_review(conn, lead, "wrong-relief", review["review_id"], "reviewer")
        assert reviews.reconcile_review(conn, lead, "replace", review["review_id"], "relief")["reviewer_id"] == "relief"


def test_other_lead_cannot_observe_escalate_or_replay_reassigned_work(team, tmp_path):
    dsn, identity = team
    manager = identity("manager")
    with connect(dsn) as conn:
        hire = TOOLS["aw_hire"][2]
        hire(conn, manager, expected_revision=1, operation_id="add-lead",
             employee_id="second-lead", role_id="review_lead", supervisor_id="manager", team_id="audit-2")
        hire(conn, manager, expected_revision=2, operation_id="add-reviewer",
             employee_id="second-reviewer", role_id="reviewer", supervisor_id="second-lead", team_id="audit-2")
        subject(conn, manager.org_id, "second-reviewer", "independent-second")
        for name in ("manager", "review-lead", "second-lead", "second-reviewer"):
            token = enroll(conn, manager.installation_id, manager.org_id, name, 1, 1)
            with os.fdopen(os.open(tmp_path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
                json.dump({"database_url": dsn, "token": token}, out)

    def request(mode):
        return {"operation_id": "request-" + mode, "artifact_ref": "text", "artifact_revision": "v1",
                "mode": mode, "risk_reason": "SAMPLED", "ttl_seconds": 3600}

    async def run():
        async with AsyncExitStack() as stack:
            manager_mcp, first, second, second_reviewer = [
                await stack.enter_async_context(live_session(tmp_path / name))
                for name in ("manager", "review-lead", "second-lead", "second-reviewer")
            ]
            async def outcome(client, tool, arguments):
                result = await client.call_tool(tool, arguments)
                return result.structuredContent

            main = await accepted(first, "aw_review_request", request("PROSE"))
            other = await accepted(first, "aw_review_request", request("SCRIPT"))
            rule = await accepted(first, "aw_review_request", request("STABILITY"))
            assert rule["state"] == "RULE_PASSED"
            main_id = {"review_id": main["review_id"]}
            observed = {"other_team_summary": await outcome(second, "aw_review_get", main_id),
                        "other_team_rule": await outcome(second, "aw_review_get", {"review_id": rule["review_id"]})}
            check = dict(main_id, operation_id="check")
            assert (await accepted(first, "aw_review_reconcile", check))["state"] == "PENDING"
            await accepted(manager_mcp, "aw_suspend", {"expected_revision": 3, "operation_id": "pause",
                                                       "employee_id": "reviewer"})
            observed["other_team_escalation"] = await outcome(second, "aw_review_reconcile",
                                           {"review_id": other["review_id"], "operation_id": "unrelated-check"})
            before = await accepted(manager_mcp, "aw_review_get", {"review_id": other["review_id"]})
            assert (await accepted(manager_mcp, "aw_review_reconcile",
                                   dict(main_id, operation_id="change-team", replacement="second-reviewer"))
                    )["reviewer_id"] == "second-reviewer"
            observed["former_lead_summary"] = await outcome(first, "aw_review_get", main_id)
            observed["former_lead_request_replay"] = await outcome(first, "aw_review_request", request("PROSE"))
            observed["former_lead_reconcile_replay"] = await outcome(first, "aw_review_reconcile", check)
            assert (await accepted(second, "aw_review_get", main_id))["reviewer_id"] == "second-reviewer"
            signature = dict(main_id, operation_id="sign", outcome="APPROVED", reason="VOICE", evidence_ref="source-A")
            signed = await accepted(second_reviewer, "aw_review_decide", signature)
            assert signed["state"] == "APPROVED" and await accepted(second_reviewer, "aw_review_decide", signature) == signed
            assert await outcome(first, "aw_review_decide", signature) == {"error": {"code": "VIEW_DENIED"}}
            assert (await accepted(first, "aw_review_get", {"review_id": rule["review_id"]}))["state"] == "RULE_PASSED"
            assert observed == {key: {"error": {"code": "REVIEW_DENIED"}} for key in observed}
            assert before["state"] == "PENDING"

    asyncio.run(run())
