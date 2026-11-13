"""Freeze public craft methods for an already-authorized runtime role and task.

This module reads only distributed runtime resources. It has no connection to
Workshop employee records, NF memories, other works, or external websites.
"""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from novel_factory.domain.canonical import fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog

_REREAD_REASONS = {
    "known_error",
    "review_failure",
    "knowledge_conflict",
    "insufficient_evidence",
    "source_changed",
}


@lru_cache(maxsize=1)
def _directory():
    root = RuntimeCatalog().root
    catalog = strict_json_loads((root / "knowledge/catalog.json").read_bytes())
    roles = strict_json_loads((root / "roles/catalog.json").read_bytes())
    return root / "knowledge", catalog, {r["role_id"]: r["task_modes"] for r in roles["roles"]}


def _verified_bytes(root: Path, relative: str, expected: str) -> bytes:
    file = (root / relative).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        raise DomainError("RUNTIME_UNAVAILABLE", "Knowledge resource is unavailable")
    data = file.read_bytes()
    if sha256(data).hexdigest() != expected:
        raise DomainError("RUNTIME_UNAVAILABLE", "Knowledge resource differs from its catalog")
    return data


@lru_cache(maxsize=4)
def _methods(root: Path, content_hash: str):
    return strict_json_loads(_verified_bytes(root, "methods.json", content_hash))


def freeze_knowledge_profile(
    role_id: str, task_kind: str, genre: str | None = None, *, reread_reason: str | None = None
) -> dict:
    """Return detached JSON. The caller persists it with the immutable task input."""
    root, catalog, roles = _directory()
    if reread_reason is not None and reread_reason not in _REREAD_REASONS:
        raise DomainError("INVALID_ARGUMENT", "Unknown knowledge reread reason")
    aliases = catalog["genres"]
    genre_id = aliases.get((genre or "").casefold().strip())
    profile = {
        "version": catalog["version"],
        "role_id": role_id,
        "task_kind": task_kind,
        "genre": genre_id,
        "reread_reason": reread_reason,
        "entries": [],
    }
    if task_kind in roles.get(role_id, []):
        methods = _methods(root, catalog["methods_sha256"])
        for item in catalog["entries"]:
            if role_id not in item["roles"] or (item["tasks"] and task_kind not in item["tasks"]):
                continue
            if item["genres"] and genre_id not in item["genres"]:
                continue
            key = item["id"]
            selected = {
                "id": key,
                "version": item["version"],
                **methods[key],
                "source_hashes": {s: catalog["sources"][s]["hash"] for s in item["sources"]},
                "original_ref": item["original_ref"],
                "original_sha256": item["original_sha256"],
            }
            if reread_reason:
                selected["original"] = _verified_bytes(
                    root, item["original_ref"], item["original_sha256"]
                ).decode("utf-8")
            profile["entries"].append(selected)
    return {**profile, "profile_hash": fingerprint(profile)}
