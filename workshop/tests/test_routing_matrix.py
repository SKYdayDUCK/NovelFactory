"""Verify every directed ordinary route and its message types in a complete Research-Audit team."""
from itertools import permutations
import json
import os
from pathlib import Path

import pytest
from agent_workshop import messaging
from agent_workshop.auth import Denied, connect
from test_messaging import team


def test_complete_ordinary_message_matrix(team):
    dsn, member = team
    default_up = {"REPORT", "ASK", "EXCEPTION", "REQUEST_REVIEW"}
    down_up = [
        ("manager", "author", {"REPORT", "REQUEST_DECISION"}, {"GOAL", "REQUEST_REVISION", "EXCEPTION", "REQUEST_REVIEW"}),
        ("manager", "script", {"ASSIGN", "REQUEST_REVISION"}, default_up),
        ("manager", "chief", {"ASSIGN", "PAUSE", "REQUEST_REVISION"}, default_up),
        ("manager", "review-lead", {"ASSIGN_REVIEW"}, default_up),
        ("manager", "ops", {"ASSIGN"}, default_up),
        ("chief", "volume", {"ASSIGN", "PAUSE", "REQUEST_REVISION"}, default_up | {"REQUEST_DECISION"}),
        ("chief", "world", {"ASSIGN"}, default_up),
        ("volume", "writer", {"ASSIGN"}, default_up),
        ("review-lead", "reviewer", {"ASSIGN_REVIEW"}, default_up - {"ASK"}),
        ("review-lead", "stats", {"ASSIGN_REVIEW"}, default_up - {"ASK"}),
    ]
    for actor in ("actor-1", "actor-2", "actor-3"):
        down_up.append(("volume", actor, {"ASSIGN", "PLAN_NOTICE"}, default_up | {"REQUEST_RESOURCE"}))
    routes = {("script", "chief"): {"PROPOSE_SCRIPT"}, ("chief", "script"): {"REQUEST_REVISION", "REPORT"}}
    for supervisor, employee, down, up in down_up:
        routes[(supervisor, employee)] = down
        routes[(employee, supervisor)] = up
    kinds = set().union(*routes.values())
    assert kinds == set(messaging.KINDS) - {"INCIDENT"}
    members = sorted({name for pair in routes for name in pair})
    evidence = {"directed_routes": [], "non_edges_checked": 0, "ACCEPTED": 0, "ROUTE_DENIED": 0}
    with connect(dsn) as conn:
        snapshot = conn.execute("SELECT src, dst, kind FROM aw_current_message_edges WHERE org_id=%s AND graph_revision=1", (member("manager").org_id,)).fetchall()
        assert {(r["src"], r["dst"], r["kind"]) for r in snapshot} == {
            (src, dst, kind) for (src, dst), allowed in routes.items() for kind in allowed
        }
        for src, dst in permutations(members, 2):
            allowed = routes.get((src, dst), set())
            observed = {}
            checked = sorted(kinds) if allowed else ["ASSIGN"]
            for kind in checked:
                args = (conn, member(src), f"{src}-{dst}-{kind}", dst, kind, "organization", 1, 300)
                if kind in allowed:
                    assert messaging.send(*args)["message_id"]
                    result = "ACCEPTED"
                else:
                    with pytest.raises(Denied, match="^ROUTE_DENIED$"):
                        messaging.send(*args)
                    result = "ROUTE_DENIED"
                observed[kind] = result
                evidence[result] += 1
            if allowed:
                evidence["directed_routes"].append({"sender": src, "recipient": dst, "messages": observed})
            else:
                evidence["non_edges_checked"] += 1
        count = conn.execute("SELECT count(*) AS n FROM aw_outbox WHERE org_id=%s", (member("manager").org_id,)).fetchone()["n"]
        assert count == evidence["ACCEPTED"]
    output = os.environ.get("AW_ROUTE_MATRIX_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2) + "\n")
