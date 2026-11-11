"""Select the NF checkout and verify both current workflows for an engineering RC."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode


WORKFLOWS = ("agent-workshop.yml", "p6-offline.yml")


def nf_source(workspace, entry, event, ref, sha):
    """Main always exercises its own release tree. Stacked PRs can use their pinned peer."""
    source = workspace
    if ref == "refs/heads/main" and event in {"push", "workflow_dispatch"}:
        checked_out = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if checked_out != sha:
            raise ValueError("Main checkout differs from the workflow event SHA")
        if not (source / entry).is_file():
            raise ValueError(f"Integrated NF entry is absent on main: {entry}")
        tracked = subprocess.check_output(["git", "-C", str(source), "ls-tree", "--name-only", sha, "--", entry],
                                          text=True).splitlines()
        if tracked != [entry] or subprocess.run(["git", "-C", str(source), "diff", "--quiet", sha, "--", entry]).returncode:
            raise ValueError(f"Integrated NF entry differs from the event commit: {entry}")
    elif not (source / entry).is_file():
        source = workspace / "nf-peer"
    if not (source / entry).is_file():
        raise ValueError(f"NF entry unavailable in selected checkout: {entry}")
    return source


def github_get(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint]))


def both_workflows_passed(repo, sha, get=github_get):
    """Qualify the newest run and current attempt for each exact main-push workflow."""
    filters = urlencode({"branch": "main", "event": "push", "head_sha": sha, "per_page": 100})
    for filename in WORKFLOWS:
        base = f"repos/{repo}/actions"
        path = f".github/workflows/{filename}"
        found = get(f"{base}/workflows/{filename}/runs?{filters}")["workflow_runs"]

        def matches(run):
            return (run["path"] == path and run["head_branch"] == "main"
                    and run["event"] == "push" and run["head_sha"] == sha)

        matching = [run for run in found if matches(run)]
        if not matching:
            return False
        latest = max(matching, key=lambda run: (run["run_number"], run["id"]))
        # Re-fetch that run. A re-run keeps the ID while replacing its current attempt.
        current = get(f"{base}/runs/{latest['id']}")
        if (current["id"] != latest["id"] or current["workflow_id"] != latest["workflow_id"]
                or not matches(current) or current["run_attempt"] < latest["run_attempt"]
                or current["status"] != "completed" or current["conclusion"] != "success"):
            return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Select the NF checkout for integrated CI")
    parser.add_argument("entry")
    args = parser.parse_args()
    try:
        print(nf_source(Path(os.environ["GITHUB_WORKSPACE"]), args.entry,
                        os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"]))
    except ValueError as error:
        sys.exit(str(error))
