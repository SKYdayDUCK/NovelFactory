"""Verify the S0 design contracts and their relationship to the deployed static runtime."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from novel_factory.contracts._schema_bundle import SCHEMAS
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "contracts/creative_harness/creative_harness.schema.json"
KINDS = {
    "CONFIGURATION", "CONTEXT_PACKET", "PLAN_CANDIDATE", "SIMULATION_REPORT",
    "REVIEW_REPORT", "ROLE_SKILL_PROPOSAL", "INSTRUCTION_CANDIDATE",
    "EXPERIMENT_FREEZE", "PROMOTION_DECISION", "EXECUTION_LOG",
}
DISABLED = {
    "enabled": False,
    "dynamic_agents": {"enabled": False},
    "adaptive_instructions": {"enabled": False, "auto_promote": False},
}
AUDITED_DIRS = (".agents/skills/", "runtime/roles/", "runtime/skills/")
AUDIT_CATEGORIES = {
    "root_instruction", "development_skill", "runtime_role", "runtime_skill",
    "runtime_catalog", "workshop", "implementation", "evidence",
}
VISIBILITY = {
    "developer_only", "runtime_selected", "runtime_metadata", "service_only",
    "historical_observation",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def named(rows, key):
    result = {}
    for row in rows:
        name = row[key]
        require(isinstance(name, str) and name and name not in result, f"duplicate or empty {key}: {name}")
        result[name] = row
    require(result, f"empty {key} collection")
    return result


def validate_examples(root, report):
    schema = read_json(root, SCHEMA)
    documents = [read_json(root, path) for path in sorted((root / "contracts").glob("*.schema.json"))]
    documents.append(schema)
    by_id = named(documents, "$id")
    registry = Registry().with_resources((uri, Resource.from_contents(doc)) for uri, doc in by_id.items())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    require(schema["$id"] not in {doc["$id"] for doc in SCHEMAS.values()},
            "S0 candidate has entered the production contract bundle")

    fixtures = {}
    for directory, expected in (("examples", True), ("invalid_examples", False)):
        rows = named(read_json(root, f"contracts/{directory}/creative_harness_s0.json"), "name")
        require(not fixtures.keys() & rows.keys(), f"reused example name in {directory}")
        fixtures.update(rows)
        report[directory] = []
        for name, row in rows.items():
            require(set(row) == {"name", "instance"}, f"bad example envelope: {name}")
            error = next(validator.iter_errors(row["instance"]), None)
            require((error is None) == expected, f"{directory}/{name}: "
                    + ("unexpectedly accepted" if error is None else f"rejected by {error.validator} at {error.json_path}"))
            report[directory].append({"name": name, "expected_valid": expected,
                                      "observed_valid": error is None})
        if expected:
            require({row["instance"]["kind"] for row in rows.values()} == KINDS,
                    "positive examples do not cover all S0 record kinds")
            positives = {name: row["instance"] for name, row in rows.items()}

    default = schema["x-default-configuration"]
    require(default == positives["default_configuration"] and default["creative_harness"] == DISABLED,
            "the explicit default example must disable all four controls")
    report["default_configuration"] = default
    actor = positives["context_packet_actor"]
    validate_world_ref(validator, actor, report)
    return actor["runtime"]


def validate_world_ref(validator, actor, report):
    live_uri = "https://novel-factory.example/contracts/2.0/core.schema.json#/$defs/WorldRef"
    deployed = validator.evolve(schema={"$ref": live_uri})
    locations = []
    for parent in ("scope", "context"):
        original = actor[parent]["world_ref"]
        require(deployed.is_valid(original), f"{parent}.world_ref violates the existing NF contract")
        bad = copy.deepcopy(actor)
        del bad[parent]["world_ref"]["world_revision_id"]
        require(not deployed.is_valid(bad[parent]["world_ref"]) and not validator.is_valid(bad),
                f"H1 {parent}.world_ref accepted an incomplete world reference")
        locations.append(f"{parent}.world_ref")
    report["world_ref"] = {"deployed_contract": live_uri, "valid_and_invalid_checked": locations}


class ObservedCatalog(RuntimeCatalog):
    def __init__(self, root):
        super().__init__(root)
        self.reads = []

    def _read(self, relative):
        self.reads.append(relative)
        return super()._read(relative)


def scoped_file(directory, relative):
    path = PurePosixPath(relative)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == relative,
            f"invalid catalog path: {relative}")
    target = directory / path
    require(target.is_file() and target.resolve().is_relative_to(directory.resolve()),
            f"catalog path leaves its resource directory: {relative}")
    return target


def check_runtime(root, binding, report):
    runtime = root / "runtime"
    roles = named(read_json(runtime, "roles/catalog.json")["roles"], "role_id")
    skills = named(read_json(runtime, "skills/catalog.json")["skills"], "name")
    catalog = ObservedCatalog(runtime)
    count = 0
    selected = set()
    matched = False
    for role_id, role in roles.items():
        role_path = scoped_file(runtime / "roles", role["contract_path"])
        modes, allowed = role["task_modes"], role["skill_names"]
        require(modes and len(modes) == len(set(modes)) and allowed and len(allowed) == len(set(allowed)),
                f"empty or duplicate mode/skill in {role_id}")
        for skill_name in allowed:
            selected.add(skill_name)
            skill = skills[skill_name]
            skill_path = scoped_file(runtime / "skills", skill["path"])
            require(skill["audience"] == "novel_runtime", f"non-runtime skill selected by {role_id}")
            sources = {"roles/catalog.json", "skills/catalog.json",
                       "roles/" + role["contract_path"], "skills/" + skill["path"]}
            for mode in modes:
                catalog.reads.clear()
                view = catalog.snapshot(role_id, mode, skill_name)
                snap = view["snapshot"]
                require(set(catalog.reads) == sources, f"unexpected runtime reads: {role_id}/{skill_name}")
                require(snap["role_contract"] == role_path.read_text(encoding="utf-8")
                        and snap["skill_instructions"] == skill_path.read_text(encoding="utf-8"),
                        f"unselected instructions in {role_id}/{skill_name}")
                require(snap["role_contract_version"] == role["contract_version"]
                        and snap["skill_version"] == skill["version"]
                        and snap["effective_model_tools"] == []
                        and view["sha256"] == fingerprint(snap), f"invalid static snapshot: {role_id}/{mode}")
                count += 1
                if (role_id, mode, skill_name) == (binding["role_id"], binding["task_kind"], binding["skill_name"]):
                    observed = {"role_version": snap["role_contract_version"], "skill_version": snap["skill_version"],
                                "runtime_sha256": view["sha256"],
                                "role_blob": git(root, "hash-object", str(role_path)).decode().strip(),
                                "skill_blob": git(root, "hash-object", str(skill_path)).decode().strip()}
                    require(all(binding[key] == value for key, value in observed.items()),
                            "H1 runtime fixture differs from the real RuntimeCatalog snapshot")
                    report["actor_snapshot"] = observed | {"role_id": role_id, "task_kind": mode, "skill_name": skill_name}
                    matched = True
    require(matched, "H1 runtime role/mode/skill was not selectable")
    require(selected == skills.keys(), "registered static skills are orphaned")
    role = roles[binding["role_id"]]
    other_skill = next(name for name in skills if name not in role["skill_names"])
    denied = [(binding["role_id"], "S0_UNREGISTERED_MODE", binding["skill_name"]),
              (binding["role_id"], binding["task_kind"], other_skill)]
    for args in denied:
        try:
            catalog.snapshot(*args)
        except DomainError as error:
            require(error.code == "ROLE_MODE_DENIED", f"unexpected rejection for {args}")
        else:
            raise ValueError(f"runtime accepted unregistered role/mode/skill combination: {args}")
    report["runtime"] = {"roles": len(roles), "skills": len(skills), "snapshots_checked": count,
                         "rejected_combinations": denied}


def validate_audit(root, report):
    audit = read_json(root, "reports/harness/S0_INSTRUCTION_SOURCES.json")
    require(type(audit["schema_version"]) is int and audit["schema_version"] == 1, "invalid audit format")
    revision = audit["source_commit"]
    require(revision == "276820316129d546b0a13118bcbe283b06507795", "wrong PRE_HARNESS_BASELINE")
    git(root, "merge-base", "--is-ancestor", revision, "HEAD")
    tree = {}
    for raw in git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if raw:
            meta, path = raw.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if kind == b"blob" and mode in {b"100644", b"100755"}:
                tree[path.decode()] = oid.decode()
    rows = named(audit["entries"], "path")
    mandatory = {p for p in tree if p in {"AGENTS.md", "SKILLS.md"} or p.startswith(AUDITED_DIRS)}
    require(mandatory <= rows.keys(), f"missing audit paths: {sorted(mandatory - rows.keys())}")
    for path, row in rows.items():
        require(tree.get(path) == row["git_blob"] and path in tree, f"wrong audited source blob: {path}")
        require(row["category"] in AUDIT_CATEGORIES and row["visibility"] in VISIBILITY,
                f"unknown source classification: {path}")
        require(all(isinstance(row[key], str) and row[key].strip() for key in ("finding", "next_stage")),
                f"empty audit finding or follow-up: {path}")
        lines = len(git(root, "cat-file", "blob", row["git_blob"]).splitlines())
        first, last = row["line_start"], row["line_end"]
        require(type(first) is int and type(last) is int and 1 <= first <= last <= lines,
                f"invalid lines for audited blob: {path}")
        if path in {"AGENTS.md", "SKILLS.md"} or path.startswith(".agents/skills/"):
            require(row["visibility"] == "developer_only", f"developer instructions misclassified: {path}")
    report["source_audit"] = {"source_commit": revision, "entries": len(rows),
                              "required_instruction_paths": len(mandatory)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON receipt, including on failure")
    args = parser.parse_args()
    report = {"stage": "P5.7-S0", "scope": "offline contract, static runtime and source audit"}
    try:
        report["checkout_sha"] = git(ROOT, "rev-parse", "HEAD").decode().strip()
        report["checkout_tree"] = git(ROOT, "rev-parse", "HEAD^{tree}").decode().strip()
        hosted_sha = os.environ.get("GITHUB_SHA")
        if hosted_sha:
            require(report["checkout_sha"] == hosted_sha, "checkout differs from workflow event SHA")
        report["event"] = os.environ.get("GITHUB_EVENT_NAME", "local")
        binding = validate_examples(ROOT, report)
        check_runtime(ROOT, binding, report)
        validate_audit(ROOT, report)
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = f"{type(error).__name__}: {error}"
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
