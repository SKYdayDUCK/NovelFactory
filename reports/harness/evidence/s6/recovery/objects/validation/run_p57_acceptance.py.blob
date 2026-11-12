"""Run repeatable P5.7 engineering gates against disposable services and retain per-case results."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "tests" / "integration"
DUAL = ROOT / "workshop" / "integration"
SCENE_EXPERIMENT = NATIVE / "test_s6_engineering_comparison.py"


def suites():
    return {
        "contracts": [
            "tests/unit",
            "tests/contracts",
            "tests/evaluation",
            "tests/service/test_admission.py",
        ],
        "harness": sorted(
            [
                *NATIVE.glob("test_harness_*.py"),
                *NATIVE.glob("test_adaptive_*.py"),
                NATIVE / "test_review_independence.py",
                NATIVE / "test_creative_metrics.py",
            ]
        ),
        "dual": [
            DUAL / f"test_{name}.py"
            for name in (
                "dual_stdio",
                "dynamic_skill_dispatch",
                "dynamic_version_switch",
                "dynamic_late_delivery",
                "creative_review_independence",
                "workshop_harness_observations",
                "workshop_harness_log_upgrade",
            )
        ],
        "faults": [
            "tests/concurrency",
            "tests/reliability",
            "tests/integration/test_agent_gateway.py",
            "tests/integration/test_p55_real_streams.py",
            "tests/integration/test_shared_token_budget.py",
        ],
        "upgrades": [
            NATIVE / f"test_{name}.py"
            for name in (
                "p2_upgrade",
                "p5_workflow_migration",
                "s9_upgrade",
                "workshop_delegation_upgrade",
                "shared_token_upgrade",
            )
        ],
        "workshops-upgrades": [
            ROOT / "workshop/acceptance" / f"test_w{stage}_historical_upgrades.py"
            for stage in (3, 4, 5, 6)
        ],
        "history": [
            *[
                NATIVE / f"test_{name}.py"
                for name in (
                    "p5_actor_tool_choice",
                    "p5_actor_handoff",
                    "p5_supervisor",
                    "p5_editorial_handoff",
                    "p5_joint_handoff",
                    "p5_supervised_role_read",
                    "gateway_narrative",
                    "agent_gateway",
                    "narrative",
                    "editorial_service",
                    "p55_real_streams",
                    "p5_role_container",
                    "s9_dependencies",
                    "s9_message_narrative",
                    "p5_process_resume",
                    "p5_workflow_migration",
                )
            ],
            "tests/queries/test_context_dependency_atomicity.py",
            "workshop/acceptance/test_w0_dual_services.py",
        ],
        "projection": ["tests/queries", "tests/service"],
        "p6": [NATIVE / "test_p6_preflight_registration.py", NATIVE / "test_p6_export_release.py"],
        "comparison": [SCENE_EXPERIMENT],
        "paired-restore": [DUAL / "test_paired_restore.py"],
        "pitr": [],
    }


def main():
    options = suites()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=options)
    parser.add_argument(
        "--output", required=True, type=Path, help="A new directory for this execution"
    )
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "AW_NF_SOURCE": str(ROOT),
        "PYTHONPATH": os.pathsep.join(
            [
                str(ROOT),
                str(ROOT / "src"),
                str(ROOT / "workshop/src"),
                str(NATIVE),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
        "NF_EVIDENCE_DIR": str(out / "recovery"),
        "NF_S6_COMPARISON_OUTPUT": str(out / "comparison.json"),
        "AW_PAIRED_RESTORE_EVIDENCE": str(out / "paired-restore.json"),
    }
    env.setdefault("AW_W0_PYTHON", sys.executable)
    if args.suite == "pitr":
        cmd = [
            sys.executable,
            "scripts/pitr_drill.py",
            "--with-redis",
            "--shared-token-budget",
            "--creative-harness",
            "--output",
            str(out / "pitr"),
        ]
    else:
        tests = [
            str(p.relative_to(ROOT) if isinstance(p, Path) else p) for p in options[args.suite]
        ]
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pyproject.toml",
            "-q",
            "--tb=short",
            "-o",
            "junit_family=xunit1",
            "--json-report",
            "--json-report-omit",
            "streams",
            "log",
            "keywords",
            "collectors",
            "--json-report-file=" + str(out / "cases.json"),
            "--junitxml=" + str(out / "cases.xml"),
            *tests,
        ]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    changed = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            "src",
            "tests",
            "contracts",
            "workshop",
            "scripts",
            "validation",
            "migrations",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    started = time.perf_counter()
    result = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
    status = result.returncode
    if not status and args.suite != "pitr":
        counts = json.loads((out / "cases.json").read_text())["summary"]
        if not counts.get("passed") or counts.get("skipped") or counts.get("xfailed"):
            status = 1
            print(
                "Required engineering suite has absent or unexecuted cases:",
                counts,
                file=sys.stderr,
            )
    receipt = {
        "suite": args.suite,
        "source_commit": commit,
        "working_tree_changes": changed,
        "command": cmd,
        "exit_code": status,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    (out / "run.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
