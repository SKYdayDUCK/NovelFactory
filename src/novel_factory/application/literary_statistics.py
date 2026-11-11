"""Versioned, branch-scoped readings of editorial and publication evidence."""

from collections import Counter
from uuid import UUID

from sqlalchemy import text

from novel_factory.application.audit_log import LogService, record
from novel_factory.application.kernel import require
from novel_factory.application.statistics import resolve_cut
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint

# Each of these commands selects an immutable revision in the same transaction.
_SELECTIONS = {
    "PLAN_ADOPT": ("plan_ref", "PLAN_REVISION"),
    "CONTINUITY_PUBLISH": ("snapshot_ref", "CONTINUITY_SNAPSHOT"),
    "RELEASE_FREEZE": ("release_ref", "RELEASE_RECORD"),
}


def record_selection(conn, command, wr, receipt, principal_id, at=None):
    ref = receipt[_SELECTIONS[command][0]]
    record(
        conn,
        "selection." + command.lower(),
        event_id=ref["artifact_revision_id"],
        evidence_id=ref["artifact_revision_id"],
        principal_id=principal_id,
        event_time=at,
        **wr,
    )


def deviation_metrics(point):
    assessment = point.get("assessment")
    if assessment is None:
        return {"world.change_points_without_assessment": 1}
    disposition = assessment["trajectory"]["disposition"]
    counts = Counter({"world.assessed_change_points": 1, "world.trajectory." + disposition: 1})
    for actor in assessment["characters"]:
        counts["world.reviewed_actor_observations"] += 1
        counts["world.actor_plausibility." + actor["plausibility"]] += 1
        if disposition == "DIVERGENCE":
            counts["world.actor_observations_on_divergent_trajectories"] += 1
    return dict(counts)


def _selected(repo, work, branch, members, command):
    ref_key, kind = _SELECTIONS[command]
    # Receipt epoch fixes the selection, even when deliveries arrive out of order.
    # Only immutable artifacts and receipts participate. No present-day pointer is read.
    scope = (
        "a.payload->'request'->>'series_id'"
        if command == "CONTINUITY_PUBLISH"
        else "a.payload->>'volume_id'"
    )
    return repo.conn.execute(
        text(f"""
        SELECT DISTINCT ON ({scope}) {scope} AS scope_id, a.payload,
          (r.payload->>'selection_epoch')::bigint AS epoch, r.payload->:ref AS ref
        FROM nf_audit_events e
        JOIN nf_artifacts a ON a.artifact_revision_id=e.evidence_id AND a.work_id=e.work_id AND a.kind=:kind
        JOIN nf_receipts r ON r.work_id=e.work_id AND r.branch_id=e.branch_id
          AND r.command_type=:command AND r.payload->:ref->>'artifact_revision_id'=a.artifact_revision_id::text
          AND r.payload->:ref->>'artifact_id'=a.artifact_id::text AND r.payload->:ref->>'sha256'=a.sha256
        WHERE e.work_id=:work AND e.branch_id=:branch AND e.event_type=:topic AND e.event_id=ANY(CAST(:members AS uuid[]))
        ORDER BY {scope}, epoch DESC
    """),
        {
            "work": work,
            "branch": branch,
            "members": members,
            "ref": ref_key,
            "kind": kind,
            "command": command,
            "topic": "selection." + command.lower(),
        },
    ).mappings()


def _goal_states(plan):
    nodes = plan["payload"]["nodes"]
    return {
        "volume_id": plan["scope_id"],
        "plan_ref": plan["ref"],
        "selection_epoch": plan["epoch"],
        "nodes": len(nodes),
        "by_status": dict(Counter(node["status"] for node in nodes)),
        "realized_with_accepted_event_refs": sum(
            node["status"] == "REALIZED" and bool(node["fulfillment_event_refs"]) for node in nodes
        ),
    }


def _foreshadow_states(series):
    snapshot = series["payload"]
    active = set(snapshot["active_thread_entries"].values())
    entries = [
        entry
        for entry in snapshot["request"]["entries"]
        if entry["entry_id"] in active and entry["kind"] == "FORESHADOW"
    ]
    return {
        "series_id": series["scope_id"],
        "snapshot_ref": series["ref"],
        "selection_epoch": series["epoch"],
        "threads": len(entries),
        "by_status": dict(Counter(entry["status"] for entry in entries)),
        "by_evidence_status": dict(
            Counter(snapshot["entry_evidence_status"][entry["entry_id"]] for entry in entries)
        ),
    }


class LiteraryStatisticsService:
    def __init__(self, kernel):
        self.kernel = kernel

    def _authorize(self, repo, principal, work, branch):
        require(branch is not None, "VIEW_DENIED", "A branch-scoped selection is required")
        self.kernel.authorize(repo, principal, work, "ADMIN", branch)
        require(
            repo.one("branches", {"work_id": work, "branch_id": branch}),
            "VIEW_DENIED",
            "Branch unavailable",
        )

    def index_history(self, principal, work, branch, *, limit=128):
        """Idempotent, bounded upgrade of exact pre-observability receipts and reports."""
        require(
            type(limit) is int and 1 <= limit <= 1000,
            "INVALID_ARGUMENT",
            "Invalid index batch size",
        )
        indexed = {}
        with self.kernel.db.uow() as repo:
            self._authorize(repo, principal, work, branch)
            args = {"work": work, "branch": branch, "limit": limit}
            for command, (key, _) in _SELECTIONS.items():
                results = (
                    repo.conn.execute(
                        text("""
                    SELECT r.payload,r.principal_id,r.created_at FROM nf_receipts r
                    WHERE r.work_id=:work AND r.branch_id=:branch AND r.command_type=:command AND NOT EXISTS (
                      SELECT FROM nf_audit_events e WHERE e.event_id=(r.payload->:key->>'artifact_revision_id')::uuid)
                    ORDER BY r.created_at,r.idempotency_key LIMIT :limit
                """),
                        {**args, "key": key, "command": command},
                    )
                    .mappings()
                    .all()
                )
                for row in results:
                    payload = row["payload"]
                    wr = (
                        payload["manifest"]["world_ref"]
                        if command == "RELEASE_FREEZE"
                        else payload["world_ref"]
                    )
                    require(
                        wr["work_id"] == str(work) and wr["branch_id"] == str(branch),
                        "INVALID_EVIDENCE",
                        "Receipt scope mismatch",
                    )
                    record_selection(
                        repo.conn, command, wr, payload, row["principal_id"], row["created_at"]
                    )
                indexed[command] = len(results)
            rows = (
                repo.conn.execute(
                    text("""
                SELECT a.artifact_revision_id,a.kind,a.payload,a.created_by,a.created_at FROM nf_artifacts a
                WHERE a.work_id=:work AND a.kind IN ('CHANGE_POINT','REVIEW_REPORT')
                  AND coalesce(a.payload->'input_world_ref'->>'branch_id',
                       a.payload->'submission'->'binding'->'world_ref'->>'branch_id')=CAST(:branch AS text)
                  AND NOT EXISTS (SELECT FROM nf_audit_events e WHERE e.event_id=a.artifact_revision_id)
                ORDER BY a.artifact_revision_id LIMIT :limit
            """),
                    args,
                )
                .mappings()
                .all()
            )
            for row in rows:
                LogService.artifact(
                    repo.conn,
                    row["kind"],
                    row["artifact_revision_id"],
                    work,
                    row["payload"],
                    row["created_by"],
                    event_time=row["created_at"],
                )
            indexed["editorial_artifacts"] = len(rows)
        return {"indexed": indexed, "more_possible": max(indexed.values()) == limit}

    def _chapters(self, repo, work, branch, members, releases, chapters):
        require(
            isinstance(chapters, list) and 0 < len(chapters) <= 256,
            "INVALID_ARGUMENT",
            "Supply up to 256 chapter bindings",
        )
        by_release = {row["ref"]["artifact_revision_id"]: row for row in releases}
        chapter_ids, assigned_drafts = set(), set()
        result = []
        for binding in chapters:
            require(
                isinstance(binding, dict)
                and set(binding) == {"chapter_id", "release_ref", "chapter_review_ref"},
                "INVALID_ARGUMENT",
                "Invalid chapter binding",
            )
            chapter_id = str(UUID(validate_contract("UUID", binding["chapter_id"])))
            require(chapter_id not in chapter_ids, "INVALID_ARGUMENT", "Repeated chapter ID")
            chapter_ids.add(chapter_id)
            release_ref = validate_contract("ArtifactRef", binding["release_ref"])
            review_ref = validate_contract("ArtifactRef", binding["chapter_review_ref"])
            release = by_release.get(release_ref["artifact_revision_id"])
            require(
                release and release["ref"] == release_ref,
                "VIEW_DENIED",
                "Release unavailable in this cut",
            )
            require(
                review_ref["artifact_revision_id"] in members,
                "VIEW_DENIED",
                "Chapter review unavailable in this cut",
            )
            report = self.kernel.artifact(repo, review_ref, work, kind="REVIEW_REPORT")["payload"]
            review = report["submission"]
            require(
                report["coverage_complete"]
                and review["scale"] == "CHAPTER"
                and review["binding"]["world_ref"]["branch_id"] == str(branch),
                "INVALID_EVIDENCE",
                "Requires an original chapter-scale review on this branch",
            )
            record_payload = release["payload"]
            approval = self.kernel.artifact(
                repo, record_payload["editorial_approval_ref"], work, kind="EDITORIAL_APPROVAL"
            )["payload"]
            require(
                review_ref in approval["review_refs"],
                "INVALID_EVIDENCE",
                "Review was not bound by this frozen edition's approval",
            )
            manifest = self.kernel.artifact(
                repo, record_payload["manifest_ref"], work, kind="RELEASE_MANIFEST"
            )["payload"]
            require(
                manifest["world_ref"] == review["binding"]["world_ref"]
                and review["binding"]["narrative_revision_id"] == manifest["narrative_revision_id"]
                and review["binding"]["arrangement_version"] == manifest["arrangement_version"]
                and review["binding"]["ordered_draft_refs"] == record_payload["ordered_draft_refs"],
                "INVALID_EVIDENCE",
                "Chapter review and frozen edition differ",
            )
            ordered = record_payload["ordered_draft_refs"]
            indices = {ref["artifact_revision_id"]: i for i, ref in enumerate(ordered)}
            selected = review["scope_draft_refs"]
            positions = [indices[ref["artifact_revision_id"]] for ref in selected]
            require(
                positions == list(range(positions[0], positions[0] + len(positions))),
                "INVALID_EVIDENCE",
                "Chapter drafts must be contiguous in the frozen edition",
            )
            chars = 0
            for ref, position in zip(selected, positions, strict=True):
                require(
                    ref == ordered[position] and ref["artifact_revision_id"] not in assigned_drafts,
                    "INVALID_ARGUMENT",
                    "Frozen draft was assigned more than once",
                )
                assigned_drafts.add(ref["artifact_revision_id"])
                draft = self.kernel.artifact(repo, ref, work, kind="NARRATIVE_DRAFT")["payload"]
                text_ref = manifest["ordered_artifacts"][position]["artifact_ref"]
                require(
                    draft["text_ref"] == text_ref,
                    "INVALID_EVIDENCE",
                    "Frozen text and draft differ",
                )
                self.kernel.artifact(repo, text_ref, work, kind="TEXT")
                chars += len(self.kernel.objects.read(text_ref["sha256"]).decode("utf-8"))
            result.append(
                {
                    **binding,
                    "volume_id": release["scope_id"],
                    "draft_count": len(selected),
                    "unicode_characters": chars,
                }
            )
        return result

    def report(self, principal, work, branch, *, chapters=None, source_cut=None, version=1):
        require(
            type(version) is int and version == 1,
            "INVALID_ARGUMENT",
            "Unknown publication report version",
        )
        with self.kernel.db.uow() as repo:
            self._authorize(repo, principal, work, branch)
            cut, members = resolve_cut(repo.conn, work, branch, source_cut)
            plans = [
                _goal_states(row) for row in _selected(repo, work, branch, members, "PLAN_ADOPT")
            ]
            series = [
                _foreshadow_states(row)
                for row in _selected(repo, work, branch, members, "CONTINUITY_PUBLISH")
            ]
            chapters_result = None
            if chapters is not None:
                releases = _selected(repo, work, branch, members, "RELEASE_FREEZE")
                chapters_result = self._chapters(
                    repo, work, branch, {str(m) for m in members}, releases, chapters
                )
            return {
                "report_version": version,
                "source_cut": cut,
                "plan_goals": plans or None,
                "foreshadow": series or None,
                "chapters": chapters_result,
                "chapter_binding_sha256": fingerprint(chapters) if chapters is not None else None,
                "autonomous_actor_deviations": None,
                "character_consistency_failure_rate": None,
                "literary_quality_score": None,
                "literary_generation_failure_rate": None,
                "unavailable_reasons": {
                    "plan_goals": "Requires an adopted plan receipt in the cut",
                    "foreshadow": "Requires a published continuity snapshot in the cut",
                    "chapters": "Requires a pinned chapter review and its matching frozen release",
                    "autonomous_actor_deviations": "Trajectory judgments cover change points; actor observations assess plausibility",
                    "character_consistency_failure_rate": "Review issues lack an actor-specific failure classification and denominator",
                    "literary_quality_score": "No calibrated literary score is registered",
                    "literary_generation_failure_rate": "No defined literary failure or eligible generation population",
                },
            }
