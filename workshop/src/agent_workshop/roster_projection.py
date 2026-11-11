"""Public organization directory and the current employee's scoped metadata."""
import json
from functools import cache
from pathlib import Path


@cache
def catalog_binding():
    # Wheels contain the frozen manifest beside this module; source checkouts
    # retain it at the existing contracts path used by operators and CI.
    path = Path(__file__).with_name("nf-catalog.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "contracts" / "nf-catalog.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        "nf_commit": manifest["nf_commit"],
        "role_catalog_hash": manifest["catalogs"]["runtime/roles/catalog.json"],
        "skill_catalog_hash": manifest["catalogs"]["runtime/skills/catalog.json"],
    }


def project_employees(visible, viewer, routes, role_modes):
    visible_ids = {member["employee_id"] for member in visible}
    catalog = catalog_binding()
    result = []
    for row in visible:
        member = dict(row)
        if member["supervisor_id"] not in visible_ids:
            member["supervisor_id"] = None
        if member["employee_id"] == viewer["employee_id"]:
            # Work modes and frozen catalog references identify suitable production roles.
            # Only NF can issue a task binding or grant for a work, character or branch.
            member["capability_profile"] = {
                "organization_role": viewer["role_id"], "nf_role_modes": list(role_modes),
            }
            member["skill_manifest_ref"] = (
                {"nf_commit": catalog["nf_commit"], "catalog_sha256": catalog["skill_catalog_hash"]}
                if role_modes else None
            )
            member["authorized_scope"] = {"organization": viewer["org_id"], "scope_ref": viewer["scope_ref"]}
            member["slot_id"] = viewer["slot_id"]
            member["allowed_routes"] = [edge for edge in routes if edge["src"] == viewer["employee_id"]]
        result.append(member)
    return result
