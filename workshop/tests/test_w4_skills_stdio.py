"""Live Workshop employee sessions keep Skill metadata, reviewer tools and task history separate."""

import asyncio
import json
import os
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from agent_workshop.reviews_skills import register_skill
from test_reviews_skills import request
from test_reviews_skills import team as review_team
from test_w1_stdio import accepted, live_session
from test_w2_stdio import denied

# Share the authenticated Research-Audit fixture with the direct PostgreSQL acceptance suite.
skill_team = review_team


def test_live_skill_scopes_and_immutable_task_history(skill_team, tmp_path):
    dsn, member = skill_team
    manager = member("manager")
    org, installation = manager.org_id, manager.installation_id
    other = uuid4().hex
    cases = (
        ("PUBLIC_SHARED", None, "writer", "foreign"),
        ("ROLE_SCOPED", "writer", "writer", "actor-1"),
        ("TEAM_SCOPED", "actors", "actor-1", "writer"),
        ("PRIVATE", "actor-1", "actor-1", "actor-2"),
        ("CONTROL_ONLY", None, "ops", "writer"),
    )
    method = b"private fixture method: never transported in a Skill response"
    with connect(dsn) as c:
        c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (other, str(uuid4())))
        bootstrap(c, other, "Research-Audit", installation_id=other)
        for revision, namespace, content in (
            ("v1", "creative", method),
            ("v2", "creative", b"new method"),
            ("control", "control", b"operator-only content"),
        ):
            register_skill(c, org, "method", revision, namespace, "PROSE", content)

    async def run():
        evidence = {}
        async with AsyncExitStack() as stack:

            async def open_employee(employee, revision=1, *, foreign=False):
                source_install, source_org = (other, other) if foreign else (installation, org)
                with connect(dsn) as c:
                    token = enroll(c, source_install, source_org, employee, revision, 1)
                path = tmp_path / f"{source_org}-{employee}-{revision}.json"
                with os.fdopen(
                    os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
                ) as out:
                    json.dump({"token": token, "database_url": dsn}, out)
                return await stack.enter_async_context(live_session(path))

            staff = {
                person: await open_employee(person)
                for person in ("manager", "writer", "actor-1", "actor-2", "ops", "reviewer")
            }
            staff["foreign"] = await open_employee("writer", foreign=True)
            tools_before = {t.name for t in (await staff["writer"].list_tools()).tools}
            assert "aw_review_decide" not in tools_before
            bindings = {}

            async def share(policy, revision, key, target=None):
                args = {
                    "operation_id": key,
                    "skill_id": "method",
                    "source_revision": revision,
                    "policy": policy,
                    "ttl_seconds": 3600,
                }
                if target is not None:
                    args["target"] = target
                return await accepted(staff["manager"], "aw_skill_share", args)

            for policy, target, allow, reject in cases:
                revision = "control" if policy == "CONTROL_ONLY" else "v1"
                binding = await share(policy, revision, policy, target)
                key = bindings[policy] = binding["binding_id"]
                args = {"binding_id": key, "mode": "PROSE"}
                # Only the hash and version leave the service, even for the authorized reader.
                expected = sha256(
                    b"operator-only content" if revision == "control" else method
                ).hexdigest()
                result = await accepted(staff[allow], "aw_skill_resolve", args)
                assert result == {
                    "skill_id": "method",
                    "source_revision": revision,
                    "content_sha": expected,
                    "mode": "PROSE",
                    "epoch": 1,
                }
                await denied(staff[reject], "aw_skill_resolve", args, "SKILL_DENIED")
                await denied(
                    staff[allow], "aw_skill_resolve", {**args, "mode": "WORLD"}, "SKILL_DENIED"
                )
                evidence[policy] = {"authorized": allow, "denied": reject, "hash": expected}
            assert {t.name for t in (await staff["writer"].list_tools()).tools} == tools_before
            decision = {
                "operation_id": "writer-decision",
                "review_id": str(uuid4()),
                "outcome": "APPROVED",
                "reason": "VOICE",
                "evidence_ref": "source-A",
            }
            await denied(staff["writer"], "aw_review_decide", decision, "VIEW_DENIED")

            review = await accepted(
                staff["manager"],
                "aw_review_request",
                {
                    "operation_id": "freeze-review",
                    "artifact_ref": "text",
                    "artifact_revision": "v1",
                    "mode": "PROSE",
                    "risk_reason": "SAMPLED",
                    "reviewer_id": "reviewer",
                    "ttl_seconds": 600,
                },
            )
            task = review["review_id"]
            freeze = {"binding_id": bindings["PUBLIC_SHARED"], "mode": "PROSE", "task_ref": task}
            frozen = await accepted(staff["reviewer"], "aw_skill_freeze", freeze)
            assert frozen["content_sha"] == sha256(method).hexdigest()
            second = await share("PUBLIC_SHARED", "v2", "method-upgrade")
            assert second["content_sha"] != frozen["content_sha"]
            await denied(
                staff["reviewer"],
                "aw_skill_freeze",
                {**freeze, "binding_id": second["binding_id"]},
                "TASK_SKILL_ALREADY_FROZEN",
            )
            revocation = await accepted(
                staff["manager"],
                "aw_skill_revoke",
                {"operation_id": "stop-method", "binding_id": freeze["binding_id"]},
            )
            assert revocation["epoch"] == frozen["epoch"] + 1
            await denied(staff["reviewer"], "aw_skill_freeze", freeze, "SKILL_DENIED")
            history = {"task_ref": task, "skill_id": "method"}
            old = await accepted(staff["reviewer"], "aw_skill_history", history)
            assert old == {
                "historical": True,
                "content_sha": frozen["content_sha"],
                "epoch": frozen["epoch"],
            }
            await denied(staff["writer"], "aw_skill_history", history, "TASK_SCOPE_DENIED")
            await denied(staff["foreign"], "aw_skill_history", history, "TASK_SCOPE_DENIED")

            transferred = await accepted(
                staff["manager"],
                "aw_transfer",
                {
                    "operation_id": "change-actor-team",
                    "expected_revision": 1,
                    "employee_id": "actor-1",
                    "role_id": "character_actor",
                    "supervisor_id": "volume",
                    "team_id": "relief",
                },
            )
            assert transferred["employment_revision"] == 2
            new_actor = await open_employee("actor-1", 2)
            for rule in ("TEAM_SCOPED", "PRIVATE"):
                lookup = {"binding_id": bindings[rule], "mode": "PROSE"}
                await denied(staff["actor-1"], "aw_skill_resolve", lookup, "STALE_EMPLOYMENT")
                await denied(new_actor, "aw_skill_resolve", lookup, "SKILL_DENIED")
            assert (
                await accepted(
                    new_actor,
                    "aw_skill_resolve",
                    {"binding_id": second["binding_id"], "mode": "PROSE"},
                )
            )["content_sha"] == second["content_sha"]

            if output := os.environ.get("AW_W4_SKILLS_EVIDENCE"):
                Path(output).write_text(
                    json.dumps(
                        {
                            "policies": evidence,
                            "frozen": frozen,
                            "live_revoke_epoch": revocation["epoch"],
                            "historical": old,
                            "next_version_hash": second["content_sha"],
                            "writer_can_approve": False,
                            "foreign_history": "TASK_SCOPE_DENIED",
                            "transferred_tenure": 2,
                        },
                        indent=2,
                    )
                    + "\n"
                )

    asyncio.run(run())


def test_history_without_a_verifiable_origin_keeps_the_snapshot_private(skill_team):
    import pytest
    from agent_workshop import reviews_skills
    from agent_workshop.auth import Denied

    dsn, member = skill_team
    org = member("manager").org_id
    with connect(dsn) as conn:
        review = request(conn, member)["review_id"]
        register_skill(conn, org, "history", "v1", "creative", "PROSE", b"history-only")
        shared = reviews_skills.share(
            conn, member("manager"), "history-share", "history", "v1", "PUBLIC_SHARED", 60
        )
        original = reviews_skills.freeze(
            conn, member("reviewer"), shared["binding_id"], "PROSE", review
        )
        # Model a partial restore inside a savepoint on this test organization's own records.
        with conn.transaction(force_rollback=True):
            conn.execute(
                "DELETE FROM aw_review_evidence WHERE org_id=%s AND review_id=%s", (org, review)
            )
            conn.execute("DELETE FROM aw_reviews WHERE org_id=%s AND review_id=%s", (org, review))
            with pytest.raises(Denied, match="TASK_SCOPE_DENIED"):
                reviews_skills.skill_history(conn, member("reviewer"), review, "history")
        assert (
            reviews_skills.skill_history(conn, member("reviewer"), review, "history")["content_sha"]
            == original["content_sha"]
        )
