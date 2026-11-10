"""Load a selected runtime role and one skill, never developer instructions.

Snapshots are saved by the application before task creation. Running tasks use
those bytes, so editing a skill cannot silently change an existing execution.
"""
from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


class RuntimeCatalog:
    def __init__(self, root: Path | None = None):
        if root is None:
            packaged = files("novel_factory").joinpath("data/runtime")
            root = Path(str(packaged)) if packaged.is_dir() else Path(__file__).resolve().parents[3] / "runtime"
        self.root = Path(root).resolve()

    def _read(self, relative: str) -> str:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise DomainError("RUNTIME_UNAVAILABLE", "Runtime resource is unavailable")
        content = path.read_text(encoding="utf-8")
        if len(content.encode("utf-8")) > 200_000:
            raise DomainError("CAPACITY_LIMIT", "Runtime resource exceeds the loading budget")
        return content

    def snapshot(self, role_id: str, task_kind: str, skill_name: str | None = None) -> dict:
        roles = json.loads(self._read("roles/catalog.json"))
        skills = json.loads(self._read("skills/catalog.json"))
        role = next((r for r in roles["roles"] if r["role_id"] == role_id), None)
        if role is None or task_kind not in role["task_modes"]:
            raise DomainError("ROLE_MODE_DENIED", "Task mode is not registered for this runtime role")
        selected = skill_name or role["skill_names"][0]
        if selected not in role["skill_names"]:
            raise DomainError("ROLE_MODE_DENIED", "Skill is outside this runtime role")
        skill = next((s for s in skills["skills"] if s["name"] == selected), None)
        if skill is None:
            raise DomainError("RUNTIME_UNAVAILABLE", "Selected runtime skill is unavailable")
        role_text = self._read("roles/" + role["contract_path"])
        skill_text = self._read("skills/" + skill["path"])
        snapshot = {
            "schema_version": "2.1",
            "role_id": role_id,
            "task_kind": task_kind,
            "role_contract_version": role["contract_version"],
            "skill_name": selected,
            "skill_version": skill["version"],
            "role_contract": role_text,
            "skill_instructions": skill_text,
            "restricted_mode": role.get("restricted_skill_modes", {}).get(selected),
            "catalog_versions": [roles["schema_version"], skills["schema_version"]],
            # Text-only model calls expose no tools. Catalog requested_tools
            # remains documentation, never an executable permission grant.
            "effective_model_tools": [],
        }
        return {"snapshot": snapshot, "sha256": fingerprint(snapshot)}
