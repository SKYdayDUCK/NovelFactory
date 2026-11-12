"""Validate the preserved design, schemas, examples and source references.

Run from any directory: python /path/to/package/validation/validate_package.py
Dependencies are pinned in requirements-validation.txt. No network calls are made.
"""
from __future__ import annotations

import argparse
import hashlib
import fnmatch
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
COUNTS: Counter = Counter()
GENERATED_DIRECTORIES = frozenset({
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".tox", ".nox", ".runtime", "build", "dist", "node_modules",
})


def package_files(pattern):
    """Walk the source package without installed dependencies or build caches.

    In particular, installed wheel copies of contracts contain documentation
    links relative to a source checkout. They are not a second design package.
    Source docs, schemas, examples, baseline and Agent/skill files stay in scope.
    """
    for directory, children, filenames in os.walk(ROOT, topdown=True):
        children[:] = sorted(name for name in children if name not in GENERATED_DIRECTORIES)
        for name in sorted(filenames):
            if fnmatch.fnmatch(name, pattern):
                yield Path(directory) / name


def check(condition, message, counter=None):
    if not condition:
        FAILURES.append(message)
    elif counter:
        COUNTS[counter] += 1


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def relative(path):
    return str(path.resolve().relative_to(ROOT))


def schema_checks():
    schemas = {}
    for path in (ROOT / "contracts").glob("*.schema.json"):
        data = read_json(path)
        Draft202012Validator.check_schema(data)
        schemas[path.name] = data
        COUNTS["schema_documents"] += 1
        COUNTS["schema_definitions"] += len(data["$defs"])
    registry = Registry().with_resources(
        (data["$id"], Resource.from_contents(data)) for data in schemas.values()
    )

    def validate(path, schema_name, definition, expected_valid):
        schema = schemas[schema_name]
        validator = Draft202012Validator(
            {"$ref": schema["$id"] + "#/$defs/" + definition},
            registry=registry,
            format_checker=FormatChecker(),
        )
        errors = list(validator.iter_errors(read_json(path)))
        check(bool(errors) != expected_valid,
              f"{relative(path)} expected {'valid' if expected_valid else 'invalid'}: "
              + "; ".join(e.message[:400] for e in errors[:3]),
              "valid_examples" if expected_valid else "invalid_examples_rejected")

    for name in ("index.json", "literary_index.json", "r1_index.json"):
        path = ROOT / "contracts/examples" / name
        index = read_json(path)
        schema_name = Path(index["schema"]).name
        for filename, definition in index["examples"].items():
            validate(path.parent / filename, schema_name, definition, True)
        for filename, definition in index.get("invalid_examples", {}).items():
            validate(path.parent / filename, schema_name, definition, False)
    path = ROOT / "contracts/invalid_examples/core_index.json"
    for filename, item in read_json(path).items():
        validate(path.parent / filename, item["schema"], item["definition"], False)

    tools = read_json(ROOT / "contracts/tool_catalog.json")["tools"]
    check(len({t["name"] for t in tools}) == len(tools), "duplicate tool names")
    for tool in tools:
        for key in ("input_schema", "output_schema", "error_schema"):
            filename, fragment = tool[key].split("#/$defs/")
            check(filename in schemas and fragment in schemas[filename]["$defs"],
                  f"unresolved {tool['name']} {key}: {tool[key]}", "tool_schema_bindings")
    COUNTS["tool_definitions"] = len(tools)
    return {t["name"] for t in tools}


def literary_evidence_checks():
    base = ROOT / "contracts/examples"
    index = read_json(base / "literary_index.json")
    artifacts = {}
    for filename, ref in index["fixture_artifacts"].items():
        raw = (base / filename).read_bytes()
        check(hashlib.sha256(raw).hexdigest() == ref["sha256"],
              f"fixture hash mismatch: {filename}", "real_artifact_hashes")
        artifacts[(ref["artifact_id"], ref["artifact_revision_id"])] = raw.decode("utf-8")

    def get_text(ref):
        return artifacts.get((ref["artifact_id"], ref["artifact_revision_id"]))

    def verify_span(span, text, where):
        if text is None:
            FAILURES.append(f"missing fixture artifact for span: {where}")
            return
        start, end = span["start"], span["end"]
        valid = (span["unit"] == "UNICODE_CODEPOINT"
                 and 0 <= start <= end <= len(text)
                 and hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()
                 == span["quote_sha256"])
        check(valid, f"invalid original-text span: {where}", "real_text_spans")

    for filename, definition in index["examples"].items():
        item = read_json(base / filename)
        if definition == "ReadingCoverage":
            text = get_text(item["artifact_ref"])
            check(text is not None and len(text) == item["source_length_codepoints"],
                  f"source length mismatch: {filename}", "reading_source_lengths")
            for span in item["spans"]:
                verify_span(span, text, filename)
        if definition == "MemoryCandidate":
            for source in item["source_refs"]:
                if source["kind"] == "ARTIFACT" and source.get("span"):
                    verify_span(source["span"], get_text(source["artifact_ref"]), filename)


def canonical_checks():
    module_path = ROOT / "experiments/test_mechanisms.py"
    spec = importlib.util.spec_from_file_location("nf_design_probes", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = read_json(ROOT / "contracts/canonicalization_vectors.json")
    for i, vector in enumerate(data["vectors"]):
        encoded = module.canonical(vector["input"])
        check(encoded.decode("utf-8") == vector["canonical_utf8"]
              and hashlib.sha256(encoded).hexdigest() == vector["sha256"],
              f"canonicalization vector {i} mismatch", "canonical_vectors")


def skill_role_checks(tool_names):
    skills = []
    for base, counter in ((".agents/skills", "development_skills"),
                          ("runtime/skills", "runtime_skills")):
        for path in (ROOT / base).glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
            check(match is not None, f"missing skill frontmatter: {relative(path)}")
            if match:
                header = yaml.safe_load(match.group(1))
                check(header.get("name") == path.parent.name
                      and isinstance(header.get("description"), str)
                      and header["description"].strip(),
                      f"invalid skill metadata: {relative(path)}", counter)
            skills.append(path)
    skill_catalog = read_json(ROOT / "runtime/skills/catalog.json")["skills"]
    skill_map = {s["name"]: s for s in skill_catalog}
    role_catalog = read_json(ROOT / "runtime/roles/catalog.json")["roles"]
    role_map = {r["role_id"]: r for r in role_catalog}
    check(len(role_map) == len(role_catalog), "duplicate role IDs")
    check(len(skill_map) == len(skill_catalog), "duplicate skill names")
    for skill in skill_catalog:
        check((ROOT / "runtime/skills" / skill["path"]).is_file(),
              f"missing skill path: {skill['path']}", "runtime_skill_paths")
    for role in role_catalog:
        role_id = role["role_id"]
        check((ROOT / "runtime/roles" / role["contract_path"]).is_file(),
              f"missing role contract: {role_id}", "role_contracts")
        check(set(role["requested_tools"]) <= tool_names,
              f"unknown requested tools for {role_id}", "role_tool_sets")
        for name in role["skill_names"]:
            check(name in skill_map, f"unknown skill {name} for {role_id}", "role_skill_bindings")
        for name, mode in role.get("restricted_skill_modes", {}).items():
            metadata = skill_map[name].get("restricted_role_modes", {}).get(role_id, {})
            check(name in role["skill_names"] and metadata.get("mode") == mode,
                  f"role/skill restricted mode mismatch: {role_id}/{name}")
            check(set(metadata.get("tools_used", [])) <= set(role["requested_tools"]),
                  f"restricted mode exceeds role requested tools: {role_id}/{name}",
                  "restricted_skill_tool_sets")
            check(metadata.get("handoff_role") in role_map,
                  f"unknown handoff role: {role_id}/{name}")
    COUNTS["agents_files"] = len(list(package_files("AGENTS.md")))
    query_profiles = read_json(ROOT / "runtime/query_profiles.json")
    profile_map = {p["role_id"]: p for p in query_profiles["profiles"]}
    check(set(profile_map) == set(role_map), "role query profile set mismatch")
    check(query_profiles["plane_capabilities"].get("PLAN") == "PLAN_READ",
          "PLAN plane must require PLAN_READ even for TIMELINE/compiler")
    for role_id, profile in profile_map.items():
        role = role_map[role_id]
        check(role.get("query_profile_id") == role_id, f"unbound query profile: {role_id}")
        if "NEIGHBORHOOD" in profile["allowed_operations"]:
            check("graph_query" in role["requested_tools"], f"new graph bypass for {role_id}")
        if "PLAN_DEPENDENTS" in profile["allowed_operations"]:
            check("plan_get" in role["requested_tools"], f"new plan bypass for {role_id}")
        if role_id == "character_actor":
            check(profile["allowed_planes"] == ["BELIEF"] and profile["actor_binding"] == "SELF_ONLY",
                  "actor query profile must stay bound to own belief")
        COUNTS["runtime_query_profiles"] += 1


def markdown_checks():
    fence = chr(96) * 3
    link_pattern = re.compile(r"!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
    for path in package_files("*.md"):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        in_fence = False
        plain = []
        for line in lines:
            if line.lstrip().startswith(fence):
                in_fence = not in_fence
            elif not in_fence:
                plain.append(line)
        check(not in_fence, f"unclosed code fence: {relative(path)}", "markdown_fences")
        content = "\n".join(plain)
        targets = [match.group(1).strip("<>") for match in link_pattern.finditer(content)]
        # Footnote bodies are prose, not reference-link destinations.
        targets += re.findall(r"^\s*\[(?!\^)[^\]]+\]:\s*(\S+)", content, flags=re.M)
        for target in targets:
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith(("#", "//")):
                continue
            if not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            check(destination.is_relative_to(ROOT) and destination.exists(),
                  f"missing local Markdown link: {relative(path)} -> {target}",
                  "local_markdown_links")
    expected_diagrams = set()
    for path in sorted((ROOT / "docs").glob("*.md")):
        pattern = re.escape(fence) + r"mermaid\s*\n(.*?)\n" + re.escape(fence)
        for number, match in enumerate(re.finditer(pattern, path.read_text(encoding="utf-8"), re.S), 1):
            filename = path.stem.split("_", 1)[0] + f"_{number:02}.mmd"
            expected_diagrams.add(filename)
            diagram = ROOT / "diagrams" / filename
            check(diagram.is_file() and diagram.read_text(encoding="utf-8") == match.group(1).strip() + "\n",
                  f"Mermaid source differs from document: {filename}", "mermaid_source_matches")
    actual_diagrams = {p.name for p in (ROOT / "diagrams").glob("*.mmd")}
    check(actual_diagrams == expected_diagrams, "unexpected or missing Mermaid source files")
    COUNTS["mermaid_sources"] = len(actual_diagrams)


def baseline_and_case_checks():
    audit = read_json(ROOT / "validation/baseline_reading_audit.json")
    base = ROOT / "baseline/v1_1"
    for item in audit["chapters"]:
        check(hashlib.sha256((base / item["source_file"]).read_bytes()).hexdigest()
              == item["source_sha256"],
              f"baseline changed: {item['source_file']}", "baseline_hashes")
    compendium = base / "小说工厂_总体架构研究与设计_完整合订本.md"
    check(hashlib.sha256(compendium.read_bytes()).hexdigest() == audit["compendium_sha256"],
          "baseline compendium changed", "baseline_hashes")
    case_path = ROOT / "validation/case_matrix.json"
    if not case_path.exists():
        FAILURES.append("missing case_matrix.json")
        return
    cases = read_json(case_path)["cases"]
    expected_legacy = ({f"T{i:02}" for i in range(1, 13)}
                       | {f"S{i:02}" for i in range(1, 9)}
                       | {f"L{i:02}" for i in range(1, 6)}
                       | {f"F{i:02}" for i in range(1, 20)})
    ids = {c["case_id"] for c in cases}
    check(len(ids) == len(cases), "duplicate case IDs")
    check(expected_legacy <= ids, "legacy case coverage incomplete")
    check(len(cases) == 60, f"expected 60 planned cases, got {len(cases)}")
    for case in cases:
        check(case["status"] == "PLANNED_NOT_EXECUTED",
              f"business case incorrectly presented as executed: {case['case_id']}",
              "planned_cases")
        source = case["source"]
        source_path = ROOT / source["path"]
        check(source_path.is_file(), f"missing case source: {case['case_id']}")
        if source_path.is_file() and source.get("file_sha256"):
            check(hashlib.sha256(source_path.read_bytes()).hexdigest() == source["file_sha256"],
                  f"case source hash changed: {case['case_id']}", "case_source_hashes")
        for path in case["v2_documents"]:
            check((ROOT / path).exists(), f"missing case mapping: {case['case_id']} -> {path}")
    COUNTS["legacy_cases_preserved"] = len(expected_legacy)
    COUNTS["additional_cases_planned"] = len(cases) - len(expected_legacy)


def r1_checks():
    matrix = read_json(ROOT / "validation/r1_case_matrix.json")
    cases = matrix["cases"]
    check({c["case_id"] for c in cases} == {"R1-" + c for c in "ABCDEFGH"}, "R1 A-H set mismatch")
    test_ids = set()
    for case in cases:
        check(case["status"] == "PLANNED_NOT_EXECUTED" and case["execution_evidence"] == [],
              f"R1 case falsely marked executed: {case['case_id']}", "r1_planned_case_families")
        for item in case["acceptance_checks"]:
            check(item["test_id"] not in test_ids, f"duplicate R1 check: {item['test_id']}")
            test_ids.add(item["test_id"])
            check(item["status"] == "PLANNED_NOT_EXECUTED" and item["execution_evidence"] == [],
                  f"R1 check falsely marked executed: {item['test_id']}", "r1_planned_acceptance_checks")
        for path in case["v2_documents"]:
            check((ROOT / path).exists(), f"missing R1 case document: {path}")
    check(len(test_ids) == 37, f"R1 expected 37 acceptance checks, got {len(test_ids)}")
    model = read_json(ROOT / "benchmarks/load_model_result.json")
    raw_input = (ROOT / "benchmarks/workload_model.json").read_bytes()
    check(model["input_sha256"] == hashlib.sha256(raw_input).hexdigest(), "load model input hash mismatch")
    check(all(model["checks"].values()), "load-model arithmetic check failed")
    COUNTS["load_model_checks"] = len(model["checks"])
    check({c["population"] for c in model["cases"]} == {10, 50, 200, 500, 1000}, "load population set mismatch")
    for case in model["cases"]:
        for profile in case["cache_cases"].values():
            check(sum(profile["role_counts"].values()) == case["population"], "role population allocation mismatch")
            check(all(profile[key] is None for key in ("accepted_commit_tps", "measured_latency_ms", "measured_cost")),
                  "arithmetic model must not claim measured service metrics", "load_model_calculated_rows")
    integration = read_json(ROOT / "validation/r1_integration_manifest.json")
    for item in integration["preserved_contracts"]:
        actual = hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest()
        check(actual == item["v2_sha256"], f"legacy contract changed: {item['path']}", "legacy_contracts_preserved")
    for item in integration["revised_primary_documents"]:
        actual = hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest()
        check(actual == item["r1_sha256"] and actual != item["v2_sha256"],
              f"primary document integration mismatch: {item['path']}", "primary_documents_integrated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "validation/package_check_results.json",
        help="Path for the JSON report (default: validation/package_check_results.json)",
    )
    output = parser.parse_args().output.resolve()
    for path in package_files("*.json"):
        if path.name == "package_check_results.json" or path == output:
            continue
        try:
            read_json(path)
            COUNTS["json_files_parsed"] += 1
        except Exception as exc:
            FAILURES.append(f"invalid JSON {relative(path)}: {exc}")
    for check_fn in (schema_checks, literary_evidence_checks, canonical_checks,
                     markdown_checks, baseline_and_case_checks, r1_checks):
        try:
            result = check_fn()
            if check_fn is schema_checks:
                skill_role_checks(result)
        except Exception as exc:
            FAILURES.append(f"{check_fn.__name__} aborted: {type(exc).__name__}: {exc}")
    result = {
        "status": "PASS" if not FAILURES else "FAIL",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Static checks of the design package, schemas, examples and source references",
        "python": platform.python_version(),
        "dependencies": {n: importlib.metadata.version(n) for n in
                         ("jsonschema", "referencing", "PyYAML")},
        "counts": dict(sorted(COUNTS.items())),
        "failures": FAILURES,
        "limitations": [
            "Markdown checks cover local file targets and Mermaid blocks. Heading anchors, external URLs and diagram rendering require separate checks.",
            "Schema examples exercise JSON structure. Database constraints, authorization and business semantics use runtime integration tests.",
            "Role and Skill checks compare the catalogs, source files and declared tool names. The runtime enforces policy when handling a request.",
            "The legacy matrices contain 60 planned business cases and 8 R1 families with 37 planned acceptance checks.",
            "The 15 historical load-model rows contain arithmetic estimates. Runtime benchmarks record measured throughput and latency.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
