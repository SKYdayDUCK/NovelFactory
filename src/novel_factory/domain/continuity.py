"""Finite cross-volume ordering and append-only literary memory rules."""
from __future__ import annotations

from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import validate_interval


def require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def validate_entries(entries, volume_count):
    by_id, latest = {}, {}
    for entry in entries:
        require(entry["entry_id"] not in by_id, "INVALID_ARGUMENT", "Continuity entry identity is duplicated")
        require(entry["volume_ordinal"] <= volume_count, "INVALID_EVIDENCE", "Entry references an absent volume")
        validate_interval(entry["story_interval"])
        require(entry["source_events"] or entry["source_texts"], "INVALID_EVIDENCE", "Continuity requires original evidence")
        if entry["plane"] == "READER":
            require(entry["kind"] == "READER_DISCLOSURE" and bool(entry["source_texts"])
                    and entry["epistemic_status"] == "TEXT_ASSERTION_NOT_WORLD_FACT",
                    "INVALID_EVIDENCE", "Reader disclosure records a textual assertion, never objective truth")
        elif entry["plane"] == "WORLD_EVIDENCE":
            require(bool(entry["source_events"]) and entry["epistemic_status"] == "EVENT_REFERENCE",
                    "INVALID_EVIDENCE", "World evidence requires accepted event references")
        else:
            require(entry["epistemic_status"] == "LITERARY_INTERPRETATION",
                    "INVALID_EVIDENCE", "Literary interpretation does not become world or actor truth")
        if entry["kind"] == "READER_DISCLOSURE":
            require(entry["plane"] == "READER", "INVALID_ARGUMENT", "Reader disclosure has a dedicated plane")
        if entry["planned_payoff_at"] is not None:
            require(entry["kind"] == "FORESHADOW" and entry["status"] != "RESOLVED",
                    "INVALID_ARGUMENT", "Future payoff is an unresolved plan, not a realized fact")
        for earlier_id in entry["supersedes_entry_ids"]:
            older = by_id.get(earlier_id)
            require(older is not None and older["thread_id"] == entry["thread_id"]
                    and older["kind"] == entry["kind"] and older["plane"] == entry["plane"]
                    and older["volume_ordinal"] <= entry["volume_ordinal"],
                    "INVALID_EVIDENCE", "Evolution must cite an earlier entry of this same thread and plane")
        if entry["thread_id"] in latest:
            require(latest[entry["thread_id"]] in entry["supersedes_entry_ids"],
                    "CONTINUITY_THREAD_CONFLICT", "A new thread state must explicitly preserve its predecessor")
        by_id[entry["entry_id"]] = entry
        latest[entry["thread_id"]] = entry["entry_id"]
    return latest


def validate_append(previous, request):
    if previous is None:
        require(request["operation"] == "APPEND" and request["revision_reason"] is None,
                "CONTINUITY_REVISION_REQUIRED", "An initial series is an append without a revision reason")
        return
    old = previous["request"]
    require(request["entries"][:len(old["entries"])] == old["entries"],
            "CONTINUITY_HISTORY_LOSS", "Keep earlier derived entries; supersede them with explicit new entries")
    if request["operation"] == "APPEND":
        require(request["ordered_release_refs"][:len(old["ordered_release_refs"])] == old["ordered_release_refs"]
                and len(request["ordered_release_refs"]) > len(old["ordered_release_refs"])
                and request["revision_reason"] is None,
                "SERIAL_PREFIX_PROTECTED", "Appending may only extend the exact published prefix")
    else:
        require(bool(request["revision_reason"]), "CONTINUITY_REVISION_REQUIRED", "A revised edition needs an explicit reason")
        require(request["ordered_release_refs"] != old["ordered_release_refs"]
                or request["world_ref"] != old["world_ref"] or request["entries"] != old["entries"],
                "CONTINUITY_NO_PROGRESS", "A revision must record a concrete new edition or interpretation")
