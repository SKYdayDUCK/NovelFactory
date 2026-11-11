"""Scope-trimmed skill metadata and bounded independent reviews. No novel bodies enter this store."""

from hashlib import sha256
from uuid import uuid4

from psycopg.types.json import Jsonb

from .auth import Denied
from .delegation import MAX_MONEY, delegate
from .messaging import ID, TTL, operation
from .organization import IDENTIFIER, NF_ROLES, employee, require_employee, schema

POLICIES = ("PUBLIC_SHARED", "ROLE_SCOPED", "TEAM_SCOPED", "PRIVATE", "CONTROL_ONLY")
MODES = ("SCRIPT", "DIRECTION", "CHARACTER", "PROSE", "WORLD", "STABILITY", "STATISTICS")
RULE_MODES = {"WORLD", "STABILITY", "STATISTICS"}
POLICY_REVISION = "aw-review.v1"
REASONS = (
    "CAUSALITY",
    "VOICE",
    "MOTIVATION",
    "SCENE_ORDER",
    "RESOURCE",
    "RELIABILITY",
    "ACCOUNTING",
    "EVIDENCE",
)
RISKS = ("SAMPLED", "MATERIAL_CHANGE", "DISCLOSURE", "RETCON", "INCIDENT", "COST_DISPUTE")


def require_role(conn, session, allowed):
    viewer = require_employee(conn, session)
    if viewer["role_id"] not in allowed:
        raise Denied("VIEW_DENIED")
    return viewer


def register_skill(conn, org, skill_id, source_revision, namespace, mode, content):
    """Operator-only content acquisition. Hash what was acquired; never persist the bytes."""
    digest = sha256(content).hexdigest()
    row = conn.execute(
        """INSERT INTO aw_skill_sources VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (org_id,skill_id,source_revision) DO UPDATE SET skill_id=EXCLUDED.skill_id
        WHERE (aw_skill_sources.content_sha,aw_skill_sources.namespace,aw_skill_sources.mode)=
            (EXCLUDED.content_sha,EXCLUDED.namespace,EXCLUDED.mode) RETURNING content_sha""",
        (org, skill_id, source_revision, digest, namespace, mode),
    ).fetchone()
    if not row:
        raise Denied("FROZEN_SOURCE_MISMATCH")
    return digest


def share(conn, session, operation_id, skill_id, source_revision, policy, ttl_seconds, target=None):
    owner = require_role(conn, session, {"workshop_manager"})
    org = session.org_id
    src = conn.execute(
        "SELECT * FROM aw_skill_sources WHERE (org_id,skill_id,source_revision)=(%s,%s,%s)",
        (org, skill_id, source_revision),
    ).fetchone()
    if (
        not src
        or policy not in POLICIES
        or (src["namespace"] == "control") != (policy == "CONTROL_ONLY")
    ):
        raise Denied("SKILL_POLICY_DENIED")
    if not 0 < ttl_seconds <= 86400 or (policy in ("PUBLIC_SHARED", "CONTROL_ONLY")) != (
        target is None
    ):
        raise Denied("SKILL_POLICY_DENIED")
    if policy == "ROLE_SCOPED" and target not in NF_ROLES:
        raise Denied("SKILL_POLICY_DENIED")
    if policy == "PRIVATE":
        peer = employee(conn, org, target)
        if not peer or peer["status"] != "ACTIVE":
            raise Denied("SKILL_POLICY_DENIED")
        # A private share is tied to the recipient's exact tenure.
        target = f"{target}@{peer['employment_revision']}"
    fields = [skill_id, source_revision, policy, ttl_seconds, target]
    with operation(conn, session, "SKILL_SHARE", operation_id, fields) as (old, out):
        if not old:
            key = uuid4()
            conn.execute(
                """INSERT INTO aw_skill_bindings(org_id,binding_id,skill_id,source_revision,content_sha,scope_ref,
                policy,target,owner,owner_revision,mode,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                clock_timestamp()+%s * interval '1 second')""",
                (
                    org,
                    key,
                    skill_id,
                    source_revision,
                    src["content_sha"],
                    owner["scope_ref"],
                    policy,
                    target,
                    owner["employee_id"],
                    owner["employment_revision"],
                    src["mode"],
                    ttl_seconds,
                ),
            )
            out.update(binding_id=str(key), content_sha=src["content_sha"], epoch=1)
        return out


def _skill(conn, session, binding_id, mode):
    viewer = require_employee(conn, session)
    row = conn.execute(
        """SELECT b.* FROM aw_skill_bindings b JOIN aw_employees e ON
        (e.org_id,e.employee_id,e.employment_revision)=(b.org_id,b.owner,b.owner_revision)
        WHERE b.org_id=%s AND binding_id=%s AND NOT revoked AND expires_at>clock_timestamp()
        AND e.status='ACTIVE' FOR SHARE OF b""",
        (session.org_id, binding_id),
    ).fetchone()
    if not row or row["scope_ref"] != viewer["scope_ref"] or row["mode"] != mode:
        raise Denied("SKILL_DENIED")
    allowed = {
        "PUBLIC_SHARED": True,
        "ROLE_SCOPED": viewer["role_id"] == row["target"],
        "TEAM_SCOPED": viewer["team_id"] == row["target"],
        "PRIVATE": f"{viewer['employee_id']}@{viewer['employment_revision']}" == row["target"],
        "CONTROL_ONLY": viewer["role_id"] in ("workshop_manager", "maintenance"),
    }
    if not allowed[row["policy"]]:
        raise Denied("SKILL_DENIED")
    return row


def resolve(conn, session, binding_id, mode):
    item = _skill(conn, session, binding_id, mode)
    return {k: item[k] for k in ("skill_id", "source_revision", "content_sha", "mode", "epoch")}


def revoke(conn, session, operation_id, binding_id):
    require_role(conn, session, {"workshop_manager"})
    with operation(conn, session, "SKILL_REVOKE", operation_id, binding_id) as (old, out):
        if not old:
            row = conn.execute(
                """UPDATE aw_skill_bindings SET epoch=epoch+CASE WHEN revoked THEN 0 ELSE 1 END,revoked=true
                WHERE org_id=%s AND binding_id=%s RETURNING epoch""",
                (session.org_id, binding_id),
            ).fetchone()
            if not row:
                raise Denied("SKILL_DENIED")
            out.update(binding_id=binding_id, epoch=row["epoch"])
        return out


def freeze(conn, session, binding_id, mode, task_ref):
    item = _skill(conn, session, binding_id, mode)
    owned = conn.execute(
        """SELECT 1 FROM aw_remote_receipts r JOIN aw_delegations d USING(org_id,operation_id)
        JOIN aw_delegation_slots s ON (s.org_id,s.slot_id)=(d.org_id,d.slot_id)
        WHERE r.org_id=%s AND r.task_id=%s AND d.installation_id=%s AND s.employee_id=%s
        AND s.employment_revision=%s AND d.state='BOUND' AND d.payload->>'task_template_id'=%s
        UNION ALL SELECT 1 FROM aw_reviews WHERE org_id=%s AND review_id=%s AND reviewer_id=%s
        AND reviewer_revision=%s AND state='PENDING' AND deadline>clock_timestamp() AND mode=%s""",
        (
            session.org_id,
            task_ref,
            session.installation_id,
            session.employee_id,
            session.employment_revision,
            mode,
            session.org_id,
            task_ref,
            session.employee_id,
            session.employment_revision,
            mode,
        ),
    ).fetchone()
    if not owned:
        raise Denied("TASK_SCOPE_DENIED")
    row = conn.execute(
        """INSERT INTO aw_skill_snapshots VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (org_id,employee_id,employment_revision,task_ref,skill_id)
        DO UPDATE SET skill_id=EXCLUDED.skill_id WHERE aw_skill_snapshots.binding_id=EXCLUDED.binding_id
        RETURNING content_sha,epoch""",
        (
            session.org_id,
            session.employee_id,
            session.employment_revision,
            task_ref,
            item["skill_id"],
            binding_id,
            item["epoch"],
            item["content_sha"],
        ),
    ).fetchone()
    if not row:
        raise Denied("TASK_SKILL_ALREADY_FROZEN")
    return {**row, "task_ref": task_ref, "skill_id": item["skill_id"]}


def skill_history(conn, session, task_ref, skill_id):
    require_employee(conn, session)
    result = conn.execute(
        """SELECT content_sha,epoch FROM aw_skill_snapshots WHERE
        (org_id,employee_id,employment_revision,task_ref,skill_id)=(%s,%s,%s,%s,%s)""",
        (session.org_id, session.employee_id, session.employment_revision, task_ref, skill_id),
    ).fetchone()
    if not result:
        raise Denied("TASK_SCOPE_DENIED")
    return {**result, "historical": True}


def _group(conn, org, employee_id):
    row = conn.execute(
        "SELECT subject_group FROM aw_review_identities WHERE org_id=%s AND employee_id=%s",
        (org, employee_id),
    ).fetchone()
    if not row:
        raise Denied("UNVERIFIED_SUBJECT")
    return row["subject_group"]


def evaluate_rules(observations):
    """Evaluate frozen, numeric observations supplied by the trusted receipt importer."""
    contracts = {
        "WORLD": {"opening", "consumed", "remaining"},
        "STABILITY": {"requests", "completed", "duplicate_effects"},
        "STATISTICS": {"incremental_tokens", "replayed_tokens"},
    }
    decisions = {}
    for mode, values in observations.items():
        if mode not in contracts or not isinstance(values, dict) or set(values) != contracts[mode]:
            raise Denied("INVALID_RULE_OBSERVATION")
        if any(
            value is not None and (type(value) is not int or value < 0) for value in values.values()
        ):
            raise Denied("INVALID_RULE_OBSERVATION")
        if None in values.values():
            decisions[mode] = "UNCERTAIN"
            continue
        if mode == "WORLD":
            passed = values["opening"] - values["consumed"] == values["remaining"]
        elif mode == "STABILITY":
            passed = values["requests"] == values["completed"] and values["duplicate_effects"] == 0
        else:
            passed = values["incremental_tokens"] == values["replayed_tokens"]
        decisions[mode] = "PASS" if passed else "FAIL"
    return decisions


def register_artifact(
    conn,
    org,
    artifact_ref,
    artifact_revision,
    digest,
    scope_ref,
    source_receipt,
    producers,
    evidence_refs,
    rule_results,
):
    """Trusted NF-receipt import. The MCP caller cannot choose authors, proxy identities or rules."""
    if (
        not producers
        or not evidence_refs
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
        or not set(rule_results).issubset(RULE_MODES)
    ):
        raise Denied("INVALID_PROVENANCE")
    rules = {"observations": rule_results, "decisions": evaluate_rules(rule_results)}
    authors = sorted(set(producers))
    groups = sorted({_group(conn, org, a) for a in authors})
    saved = conn.execute(
        """INSERT INTO aw_review_artifacts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (org_id,artifact_ref,artifact_revision) DO UPDATE SET content_sha=EXCLUDED.content_sha WHERE
        (aw_review_artifacts.content_sha,aw_review_artifacts.scope_ref,aw_review_artifacts.source_receipt,
         aw_review_artifacts.producer_groups,aw_review_artifacts.producer_ids,aw_review_artifacts.evidence_refs,aw_review_artifacts.rule_results)=
        (EXCLUDED.content_sha,EXCLUDED.scope_ref,EXCLUDED.source_receipt,EXCLUDED.producer_groups,EXCLUDED.producer_ids,
         EXCLUDED.evidence_refs,EXCLUDED.rule_results) RETURNING source_receipt""",
        (
            org,
            artifact_ref,
            artifact_revision,
            digest,
            scope_ref,
            source_receipt,
            groups,
            authors,
            sorted(set(evidence_refs)),
            Jsonb(rules),
        ),
    ).fetchone()
    if not saved:
        raise Denied("FROZEN_SOURCE_MISMATCH")


def _independent(conn, org, reviewer, artifact):
    if (
        reviewer["employee_id"] in artifact["producer_ids"]
        or _group(conn, org, reviewer["employee_id"]) in artifact["producer_groups"]
    ):
        raise Denied("INDEPENDENT_REVIEW_REQUIRED")


def review_request(
    conn,
    session,
    operation_id,
    artifact_ref,
    artifact_revision,
    mode,
    risk_reason,
    ttl_seconds,
    reviewer_id=None,
    slot_id=None,
    max_amount="0",
    token_limit=0,
):
    viewer = require_role(conn, session, {"workshop_manager", "review_lead"})
    org = session.org_id
    fields = [
        artifact_ref,
        artifact_revision,
        mode,
        risk_reason,
        ttl_seconds,
        reviewer_id,
        slot_id,
        max_amount,
        token_limit,
    ]
    if mode not in MODES or risk_reason not in RISKS or not 0 < ttl_seconds <= 86400:
        raise Denied("INVALID_REVIEW")
    with operation(conn, session, "REVIEW_REQUEST", operation_id, fields) as (old, out):
        if old:
            return out
        a = conn.execute(
            """SELECT * FROM aw_review_artifacts WHERE (org_id,artifact_ref,artifact_revision,scope_ref)=(%s,%s,%s,%s)
            FOR SHARE""",
            (org, artifact_ref, artifact_revision, viewer["scope_ref"]),
        ).fetchone()
        if not a:
            raise Denied("EVIDENCE_SCOPE_DENIED")
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,2))", (org + a["content_sha"] + mode,)
        )
        last = conn.execute(
            """SELECT * FROM aw_reviews WHERE org_id=%s AND content_sha=%s AND mode=%s AND policy_revision=%s
            ORDER BY round_no DESC LIMIT 1""",
            (org, a["content_sha"], mode, POLICY_REVISION),
        ).fetchone()
        if last and (last["state"] != "CHANGES_REQUESTED" or last["round_no"] >= 2):
            raise Denied("REVIEW_ALREADY_ASSIGNED_OR_FINAL")
        n = 1 if not last else 2
        result = a["rule_results"]["decisions"].get(mode, "UNCERTAIN")
        state, execution, reviewer, remote = "PENDING", "HUMAN", None, None
        if result in ("PASS", "FAIL") and (result == "FAIL" or risk_reason == "SAMPLED"):
            state, execution = ("RULE_PASSED" if result == "PASS" else "RULE_BLOCKED"), "RULE"
            if slot_id or max_amount != "0" or token_limit:
                raise Denied("RULE_REQUIRES_NO_MODEL")
        else:
            if not reviewer_id:
                candidate = conn.execute(
                    """SELECT e.employee_id FROM aw_employees e JOIN aw_review_identities i
                    USING (org_id,employee_id) WHERE e.org_id=%s AND e.scope_ref=%s AND e.role_id='reviewer'
                    AND e.status='ACTIVE' AND NOT (i.subject_group=ANY(%s)) AND NOT (e.employee_id=ANY(%s))
                    ORDER BY (SELECT count(*) FROM aw_reviews pending WHERE pending.org_id=e.org_id AND
                         pending.reviewer_id=e.employee_id AND pending.state='PENDING'),e.employee_id LIMIT 1""",
                    (org, viewer["scope_ref"], a["producer_groups"], a["producer_ids"]),
                ).fetchone()
                reviewer_id = candidate["employee_id"] if candidate else None
            reviewer = employee(conn, org, reviewer_id)
            if (
                not reviewer
                or reviewer["role_id"] != "reviewer"
                or reviewer["status"] != "ACTIVE"
                or reviewer["scope_ref"] != viewer["scope_ref"]
            ):
                raise Denied("INDEPENDENT_REVIEW_REQUIRED")
            _independent(conn, org, reviewer, a)
            if slot_id:
                slot = conn.execute(
                    """SELECT s.slot_id FROM aw_review_slots r JOIN aw_delegation_slots s USING(org_id,slot_id)
                    WHERE (r.org_id,r.slot_id,r.artifact_ref,r.artifact_revision,r.mode)=(%s,%s,%s,%s,%s)
                    AND s.employee_id=%s AND s.employment_revision=%s AND s.config->>'role_id'='reviewer'""",
                    (
                        org,
                        slot_id,
                        artifact_ref,
                        artifact_revision,
                        mode,
                        reviewer_id,
                        reviewer["employment_revision"],
                    ),
                ).fetchone()
                if not slot or token_limit <= 0:
                    raise Denied("REVIEW_SLOT_DENIED")
                remote = delegate(
                    conn,
                    session,
                    "review-" + str(uuid4()),
                    slot_id,
                    max_amount,
                    token_limit,
                    _review_request=True,
                )["operation_id"]
                execution = "BUDGETED_NF"
            elif max_amount != "0" or token_limit:
                raise Denied("REVIEW_SLOT_DENIED")
        key = uuid4()
        conn.execute(
            """INSERT INTO aw_reviews(org_id,review_id,artifact_ref,artifact_revision,content_sha,mode,round_no,
            policy_revision,requested_by,risk_reason,reviewer_id,reviewer_revision,execution,state,deadline,operation_id,
            budget_amount,token_limit) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second',%s,%s,%s)""",
            (
                org,
                key,
                artifact_ref,
                artifact_revision,
                a["content_sha"],
                mode,
                n,
                POLICY_REVISION,
                session.employee_id,
                risk_reason,
                reviewer["employee_id"] if reviewer else None,
                reviewer["employment_revision"] if reviewer else None,
                execution,
                state,
                ttl_seconds,
                remote,
                max_amount,
                token_limit,
            ),
        )
        conn.execute(
            "INSERT INTO aw_review_evidence(org_id,review_id,seq,authority,reason,evidence_ref) VALUES (%s,%s,1,%s,%s,%s)",
            (org, key, execution, result, a["source_receipt"]),
        )
        out.update(review_id=str(key), state=state, round_no=n, execution=execution)
        if remote:
            out["operation_id"] = remote
        return out


def decide(conn, session, operation_id, review_id, outcome, reason, evidence_ref):
    viewer = require_role(conn, session, {"reviewer"})
    if outcome not in ("APPROVED", "CHANGES_REQUESTED") or reason not in REASONS:
        raise Denied("INVALID_REVIEW")
    with operation(
        conn, session, "REVIEW_DECIDE", operation_id, [review_id, outcome, reason, evidence_ref]
    ) as (old, out):
        if old:
            return out
        r = conn.execute(
            """SELECT *,deadline>clock_timestamp() AS live FROM aw_reviews
            WHERE org_id=%s AND review_id=%s AND reviewer_id=%s AND reviewer_revision=%s FOR UPDATE""",
            (session.org_id, review_id, session.employee_id, session.employment_revision),
        ).fetchone()
        if not r or r["state"] != "PENDING":
            raise Denied("REVIEW_DENIED")
        a = conn.execute(
            """SELECT * FROM aw_review_artifacts WHERE org_id=%s AND artifact_ref=%s AND artifact_revision=%s
            AND scope_ref=%s""",
            (session.org_id, r["artifact_ref"], r["artifact_revision"], viewer["scope_ref"]),
        ).fetchone()
        if not a or evidence_ref not in a["evidence_refs"]:
            raise Denied("EVIDENCE_SCOPE_DENIED")
        _independent(conn, session.org_id, viewer, a)
        if not r["live"]:
            state = "ESCALATED"
            reason = "DEADLINE"
        else:
            if (
                r["execution"] == "BUDGETED_NF"
                and not conn.execute(
                    """SELECT 1 FROM aw_remote_receipts r
                JOIN aw_budget_holds h USING(org_id,operation_id) WHERE r.org_id=%s AND r.operation_id=%s
                AND h.status='SETTLED' AND r.accounting_state='SETTLED' AND r.task_state='ACCEPTED'""",
                    (session.org_id, r["operation_id"]),
                ).fetchone()
            ):
                raise Denied("WAITING_NF_RECONCILIATION")
            state = (
                "ESCALATED" if outcome == "CHANGES_REQUESTED" and r["round_no"] == 2 else outcome
            )
        conn.execute(
            "UPDATE aw_reviews SET state=%s,decision_reason=%s,evidence_ref=%s WHERE org_id=%s AND review_id=%s",
            (state, reason, evidence_ref, session.org_id, review_id),
        )
        conn.execute(
            "INSERT INTO aw_review_evidence(org_id,review_id,seq,authority,reason,evidence_ref) VALUES (%s,%s,2,%s,%s,%s)",
            (session.org_id, review_id, session.employee_id, reason, evidence_ref),
        )
        out.update(review_id=review_id, state=state)
        return out


TOOLS = {
    "aw_skill_history": (
        "Read a frozen historical hash.",
        schema({"task_ref": ID, "skill_id": IDENTIFIER}),
        skill_history,
    ),
    "aw_skill_share": (
        "Share registered skill metadata under an explicit scope.",
        schema(
            {
                "operation_id": IDENTIFIER,
                "skill_id": IDENTIFIER,
                "source_revision": IDENTIFIER,
                "policy": {"enum": list(POLICIES)},
                "ttl_seconds": TTL,
                "target": IDENTIFIER,
            },
            ["operation_id", "skill_id", "source_revision", "policy", "ttl_seconds"],
        ),
        share,
    ),
    "aw_skill_revoke": (
        "Revoke live skill use.",
        schema({"operation_id": IDENTIFIER, "binding_id": ID}),
        revoke,
    ),
    "aw_skill_resolve": (
        "Resolve live skill metadata without expanding NF grants.",
        schema({"binding_id": ID, "mode": IDENTIFIER}),
        resolve,
    ),
    "aw_skill_freeze": (
        "Freeze skill provenance for this employee's task.",
        schema({"binding_id": ID, "mode": IDENTIFIER, "task_ref": ID}),
        freeze,
    ),
    "aw_review_request": (
        "Create an independent scoped review, evaluating trusted deterministic rules first.",
        schema(
            {
                "operation_id": IDENTIFIER,
                "artifact_ref": IDENTIFIER,
                "artifact_revision": IDENTIFIER,
                "mode": {"enum": list(MODES)},
                "risk_reason": {"enum": list(RISKS)},
                "ttl_seconds": TTL,
                "reviewer_id": IDENTIFIER,
                "slot_id": IDENTIFIER,
                "max_amount": MAX_MONEY,
                "token_limit": {"type": "integer", "minimum": 0, "maximum": 10000000},
            },
            [
                "operation_id",
                "artifact_ref",
                "artifact_revision",
                "mode",
                "risk_reason",
                "ttl_seconds",
            ],
        ),
        review_request,
    ),
    "aw_review_decide": (
        "Record a bounded decision on a frozen artifact and authorized evidence.",
        schema(
            {
                "operation_id": IDENTIFIER,
                "review_id": ID,
                "outcome": {"enum": ["APPROVED", "CHANGES_REQUESTED"]},
                "reason": {"enum": list(REASONS)},
                "evidence_ref": IDENTIFIER,
            }
        ),
        decide,
    ),
}
