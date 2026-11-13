"""Optional chapter attribution and user-priced estimates over pinned, scoped evidence."""

import re
from collections import defaultdict
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.application.statistics import calculate, resolve_cut
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError

_CHAPTERS = "REPORT_CHAPTER_SCENES"
_PRICES = "REPORT_MODEL_PRICES"
_RATE_FIELDS = {"input_per_million", "cached_input_per_million", "output_per_million"}
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,9})?\Z")
_KINDS = {"chapters": _CHAPTERS, "prices": _PRICES}
_COST_KEYS = {
    "model.fixture.cost.": "fixture",
    "model.provider_reported_charge.": "provider_reported",
    "model.host_reported_amount.": "host_reported",
}


def _valid(test):
    require(test, "INVALID_ARGUMENT", "Invalid chapter map or price book")


def _id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise DomainError("INVALID_ARGUMENT", "Expected UUID") from error


def _validate(body, work, branch):
    _valid(isinstance(body, dict) and isinstance(body.get("kind"), str) and body["kind"] in _KINDS)
    kind = body["kind"]
    rows = "chapters" if kind == "chapters" else "prices"
    _valid(
        set(body)
        == {"schema_version", "kind", "document_id", "version", "work_id", "branch_id", rows}
    )
    _valid(
        type(body["schema_version"]) is int
        and body["schema_version"] == 1
        and type(body["version"]) is int
        and 1 <= body["version"] <= 2**31 - 1
    )
    _id(body["document_id"])
    require(
        _id(body["work_id"]) == str(work) and _id(body["branch_id"]) == str(branch),
        "VIEW_DENIED",
        "Report scope unavailable",
    )
    _valid(isinstance(body[rows], list) and 1 <= len(body[rows]) <= 4096)
    _valid(len(canonical_bytes(body)) <= 2_000_000)
    seen = set()
    owners = {}
    for row in body[rows]:
        _valid(isinstance(row, dict))
        if kind == "chapters":
            _valid(set(row) == {"chapter_id", "volume_id", "scene_ids"})
            key = _id(row["chapter_id"])
            _id(row["volume_id"])
            _valid(isinstance(row["scene_ids"], list) and bool(row["scene_ids"]))
            for scene in row["scene_ids"]:
                _valid(owners.setdefault(_id(scene), key) == key)
        else:
            _valid(
                set(row)
                in (
                    {"model", "currency"} | _RATE_FIELDS,
                    {"model", "provider", "currency"} | _RATE_FIELDS,
                )
            )
            _valid(isinstance(row["model"], str) and 0 < len(row["model"]) <= 256)
            if "provider" in row:
                _valid(isinstance(row["provider"], str) and 0 < len(row["provider"]) <= 256)
            _valid(isinstance(row["currency"], str) and re.fullmatch("[A-Z]{3}", row["currency"]))
            for field in _RATE_FIELDS:
                _valid(isinstance(row[field], str) and _AMOUNT.fullmatch(row[field]))
            key = (row.get("provider"), row["model"])
        _valid(key not in seen)
        seen.add(key)


def _check_scenes(conn, chapters, work, branch):
    requested = {_id(scene): _id(ch["volume_id"]) for ch in chapters for scene in ch["scene_ids"]}
    known = conn.execute(
        text("""SELECT DISTINCT tt.trace_id,tt.volume_id FROM nf_trace_tasks tt
        JOIN nf_tasks t USING(task_id) WHERE t.work_id=:w AND t.branch_id=:b AND tt.trace_id=ANY(CAST(:ids AS uuid[]))"""),
        {"w": work, "b": branch, "ids": list(requested)},
    ).all()
    require(
        {(str(scene), str(volume)) for scene, volume in known} == set(requested.items()),
        "VIEW_DENIED",
        "Unavailable chapter scene or volume",
    )


def _amount(values):
    return {
        kind: {cur: format(v, "f") for cur, v in sorted(amounts.items())}
        for kind, amounts in sorted(values.items())
    }


def estimate_usage_cost(usage, rate):
    if rate is None:
        return None, "MISSING_RATE"
    inp, out, cached = (
        usage.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens")
    )
    if any(type(v) is not int or not 0 <= v <= 2**53 - 1 for v in (inp, out)):
        return None, "MISSING_OR_INVALID_USAGE"
    std, discounted, output = (
        Decimal(rate[k])
        for k in ("input_per_million", "cached_input_per_million", "output_per_million")
    )
    if cached is None and std == discounted:
        cached = 0  # No billing distinction. The number of cached tokens remains unknown.
    if type(cached) is not int or not 0 <= cached <= inp:
        return None, "MISSING_OR_INVALID_CACHE_USAGE"
    with localcontext() as context:
        context.prec = 64
        return ((inp - cached) * std + cached * discounted + out * output) / Decimal(
            1_000_000
        ), None


class ChapterCostService:
    def __init__(self, kernel):
        self.kernel = kernel

    def pin(self, principal, work, branch, body):
        _validate(body, work, branch)
        kind = _KINDS[body["kind"]]
        document = uuid5(UUID(str(branch)), kind + ":" + _id(body["document_id"]))
        revision = str(uuid5(document, str(body["version"])))
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, principal, work, "ADMIN", branch)
            if kind == _CHAPTERS:
                _check_scenes(repo.conn, body["chapters"], work, branch)
            repo.conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:id,0))"), {"id": revision}
            )
            prior = repo.one("artifacts", {"artifact_revision_id": revision})
            if prior:
                require(
                    prior["kind"] == kind
                    and prior["work_id"] == work
                    and prior["sha256"] == fingerprint(body),
                    "IDEMPOTENCY_CONFLICT",
                    "Report input version is already pinned",
                )
                return {k: prior[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
            ref = self.kernel._blob(body)
            ref.update(artifact_id=str(document), artifact_revision_id=revision)
            self.kernel._register_artifact(repo, ref, work, body, kind, principal.principal_id)
            return ref

    def report(self, principal, work, branch, chapter_ref, price_ref=None, *, source_cut=None):
        with self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, principal, work, "ADMIN", branch)

            def load(ref, kind):
                body = self.kernel.artifact(repo, ref, work, kind=kind)["payload"]
                require(
                    _id(body["branch_id"]) == str(branch)
                    and body["schema_version"] == 1
                    and fingerprint(body) == ref["sha256"],
                    "VIEW_DENIED",
                    "Pinned report input unavailable",
                )
                return body

            mapping = load(chapter_ref, _CHAPTERS)
            prices = load(price_ref, _PRICES) if price_ref else None
            _check_scenes(repo.conn, mapping["chapters"], work, branch)
            rates = {(r.get("provider"), r["model"]): r for r in prices["prices"]} if prices else {}
            # The same cut/lock/retention logic as the incremental verifier. The
            # report reads immutable evidence, even if the statistics worker is offline.
            cut, members = resolve_cut(repo.conn, work, branch, source_cut)
            by_scene = {
                _id(scene): c["chapter_id"] for c in mapping["chapters"] for scene in c["scene_ids"]
            }
            events = repo.conn.execute(
                text("""SELECT a.*, f.payload FROM nf_audit_events a
                LEFT JOIN nf_artifacts f ON f.work_id=a.work_id AND f.artifact_revision_id=a.evidence_id
                  AND a.event_type IN ('model.completed','artifact.agent_role_tool_response')
                WHERE a.event_id=ANY(CAST(:members AS uuid[])) AND a.work_id=:work AND a.branch_id=:branch
                  AND a.trace_id=ANY(CAST(:scenes AS uuid[]))
                  AND a.event_type IN ('model.completed','artifact.agent_role_tool_response',
                                       'artifact.agent_prompt','artifact.agent_role_tool_prompt')
                ORDER BY a.event_time,a.event_id"""),
                {"work": work, "branch": branch, "scenes": list(by_scene), "members": members},
            ).mappings()
            grouped = defaultdict(list)
            for event in events:
                grouped[by_scene[str(event["trace_id"])]].append(event)
            chapters = []
            for chapter in mapping["chapters"]:
                scenes = list(dict.fromkeys(_id(scene) for scene in chapter["scene_ids"]))
                chapters.append(
                    {
                        "chapter_id": chapter["chapter_id"],
                        "volume_id": chapter["volume_id"],
                        "scene_ids": scenes,
                        **self._chapter(repo.conn, grouped[chapter["chapter_id"]], rates),
                    }
                )
            return {
                "report_version": 1,
                "metric_version": 3,
                "source_cut": cut,
                "chapter_map_ref": chapter_ref,
                "chapter_map_version": mapping["version"],
                "price_book_ref": price_ref,
                "price_book_version": prices["version"] if prices else None,
                "chapters": chapters,
                "invoiced_supplier_cost": None,
            }

    @staticmethod
    def _chapter(conn, events, rates):
        prepared = {}
        completed = set()
        reported = defaultdict(lambda: defaultdict(Decimal))
        estimates = defaultdict(lambda: defaultdict(Decimal))
        reasons = defaultdict(int)
        evidence = []
        missing_estimate = missing_amount = 0
        for event in events:
            attempt = str(event["attempt_id"])
            if event["event_type"] in {"artifact.agent_prompt", "artifact.agent_role_tool_prompt"}:
                prepared[attempt] = str(event["event_id"])
                continue
            # One accepted identity per attempt. Distinct phases have distinct ledger IDs.
            require(
                attempt not in completed,
                "INCONSISTENT_EVIDENCE",
                "Multiple terminal records for one call",
            )
            completed.add(attempt)
            data = event["payload"]
            evidence.append(
                {
                    "event_id": str(event["event_id"]),
                    "attempt_id": attempt,
                    "artifact_revision_id": str(event["evidence_id"]),
                }
            )
            metrics = calculate(conn, event, 3)
            received = False
            for prefix, kind in _COST_KEYS.items():
                for key, value in metrics.items():
                    if key.startswith(prefix):
                        with localcontext() as context:
                            context.prec = 64
                            reported[kind][key.removeprefix(prefix)] += Decimal(str(value))
                        received = True
            if not received:
                missing_amount += 1
                reasons["NO_CONFIRMED_GENERATION_AMOUNT"] += 1
            config = data.get("model_configuration") or {}
            rate = rates.get(
                (config.get("provider"), config.get("model")),
                rates.get((None, config.get("model"))),
            )
            value, uncertain = estimate_usage_cost(data.get("usage", data), rate)
            source = next(
                (k for k in ("fixture", "reported") if "model." + k + ".calls" in metrics),
                "unclassified",
            )
            if uncertain or source == "unclassified":
                missing_estimate += 1
                reasons[uncertain or "UNKNOWN_PROVENANCE"] += 1
            else:
                with localcontext() as context:
                    context.prec = 64
                    estimates[source][rate["currency"]] += value
        unresolved = sorted(set(prepared) - completed)
        if unresolved:
            reasons["PREPARED_WITHOUT_COMPLETION"] = len(unresolved)
        return {
            "observed_completed_calls": len(completed),
            "calls_without_completion": unresolved,
            "terminal_evidence": evidence,
            "known_reported_amounts": _amount(reported),
            "reported_amounts": _amount(reported)
            if completed and not (missing_amount or unresolved)
            else None,
            "known_estimated_amounts": _amount(estimates),
            "estimated_amounts": _amount(estimates)
            if completed and not (missing_estimate or unresolved)
            else None,
            "uncertainty": dict(reasons) if completed or unresolved else {"NO_RECORDED_CALLS": 1},
        }
