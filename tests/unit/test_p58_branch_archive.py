"""Real Git objects and atomically updated bare remotes, without contacting GitHub."""

import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "scripts/p58_branch_archive.py"
spec = importlib.util.spec_from_file_location("p58_archive", MODULE)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


@pytest.fixture
def state(tmp_path, monkeypatch):
    work, remote = tmp_path / "work", tmp_path / "remote.git"
    archive.git("init", "-b", "main", work)
    archive.git("init", "--bare", "-b", "main", remote)
    monkeypatch.chdir(work)

    def commit(message):
        archive.git("add", "-A")
        archive.git(
            "-c",
            "user.name=Archive Test",
            "-c",
            "user.email=test@example.org",
            "commit",
            "-m",
            message,
        )
        return archive.git("rev-parse", "HEAD").decode().strip()

    workflow = Path(".github/workflows/run.yml")
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: {push: {branches: [operational, 'dev/maint-*']}}\n")
    cited = Path("reports/historic.md")
    cited.parent.mkdir()
    cited.write_text(
        f"[measurement](https://github.com/{archive.REPO}/blob/evidence/fixture/report.md)\n"
    )
    base = commit("current production line")
    for stable in [
        "original",
        "operational",
        "dev/maint-a",
        "protected",
        "scion-pr",
        "active-run",
        "discussed",
        "evidence/fixture",
    ]:
        archive.git("branch", stable, base)
    archive.git("switch", "-c", "history")
    historical = Path("deleted-evidence.txt")
    historical.write_text("old evidence\n")
    historical_commit = commit("independent measurement")
    blob = archive.git("rev-parse", "HEAD:deleted-evidence.txt").decode().strip()
    historical.unlink()
    Path("notes.txt").write_text("superseded run\n")
    tip = commit("remove historical measurement from tip")
    archive.git("switch", "main")
    archive.git("remote", "add", "origin", remote)
    archive.git("push", "origin", "--all")
    tips = archive.git_refs(str(remote))
    meta = {
        "repo": archive.REPO,
        "discussion_references": {
            "discussed": ["https://github.com/" + archive.REPO + "/issues/1"]
        },
        "branches": {
            name: {"oid": oid, "protected": name == "protected"} for name, oid in tips.items()
        },
        "pulls": [
            {
                "number": 4,
                "base": {"repo": archive.REPO, "branch": "main", "oid": base},
                "head": {"repo": archive.REPO, "branch": "scion-pr", "oid": base},
            }
        ],
        "workflow_runs": [{"id": 23, "branch": "active-run", "oid": base}],
    }
    inv = archive.inventory(meta)
    plan = archive.make_plan(inv)
    return work, remote, inv, plan, meta, base, historical_commit, blob, tip


def test_inventory_history_and_fresh_restoration(tmp_path, state):
    work, remote, inv, plan, _, base, historical_commit, blob, tip = state
    rows = {b["name"]: b for b in inv["branches"]}
    assert rows["original"]["class"] == "A"
    assert rows["history"]["class"] == "B"
    assert rows["history"]["merge_bases"] == [base]
    assert rows["history"]["independent_commits"] == [historical_commit, tip]
    assert {
        "oid": blob,
        "bytes": 13,
        "historical_path": "deleted-evidence.txt",
    } in rows["history"]["history_blobs_unreachable_from_main"]
    for name in [
        "main",
        "operational",
        "dev/maint-a",
        "protected",
        "scion-pr",
        "active-run",
        "discussed",
        "evidence/fixture",
    ]:
        assert rows[name]["class"] == "C"
    assert rows["operational"]["references_in_tracked_text"][".github/workflows/run.yml"] == [1]
    assert rows["evidence/fixture"]["evidence_urls_by_tracked_path"] == {"reports/historic.md": [1]}
    assert set(plan["delete_after_verified_restore"]) == {"original", "history"}
    archive.validate(plan, inv)
    bundle = archive.prepare_bundle(work, plan, inv, tmp_path / "bundles")
    # Remove both original historical refs from the real isolated remote before restoration.
    archive.delete_reviewed(str(remote), plan["delete_after_verified_restore"])
    assert set(archive.git_refs(str(remote))) == set(plan["retained"])
    dest = tmp_path / "restore"
    dest.mkdir()
    assert archive.reconstruct(remote.as_uri(), plan, inv, bundle, dest) == len(rows)
    assert (
        archive.git(
            "show",
            historical_commit + ":deleted-evidence.txt",
            cwd=dest / "recovered.git",
        )
        == b"old evidence\n"
    )
    assert not (dest / "recovered.git/objects/info/alternates").exists()


def test_fail_closed_before_any_ref_change(state):
    _, remote, inv, plan, meta, base, _, _, tip = state
    candidates = archive.final_gate(meta, plan, inv)
    bad = copy.deepcopy(plan)
    bad["delete_after_verified_restore"]["scion-pr"] = bad["retained"].pop("scion-pr")
    with pytest.raises(RuntimeError, match="exceeds"):
        archive.validate(bad, inv)
    changed = copy.deepcopy(meta)
    changed["branches"]["history"]["oid"] = base
    with pytest.raises(RuntimeError, match="history"):
        archive.final_gate(changed, plan, inv)
    new_pr = copy.deepcopy(meta)
    new_pr["pulls"][0]["head"]["branch"] = "history"
    with pytest.raises(RuntimeError, match=r"PR #4|history"):
        archive.final_gate(new_pr, plan, inv)
    archive.git("update-ref", "refs/heads/original", tip, base, cwd=remote)
    before = archive.git_refs(str(remote))
    with pytest.raises(subprocess.CalledProcessError):
        archive.delete_reviewed(str(remote), candidates)
    # Git rejects the *whole* batch on a stale expected OID, including history's valid deletion.
    assert archive.git_refs(str(remote)) == before


def test_tag_and_truncated_archive_refused(tmp_path, state):
    work, remote, inv, plan, _, base, _, _, tip = state
    archive.git("update-ref", "refs/tags/archive", base, cwd=remote)
    archive.archive_tag_matches(str(remote), "archive", base)
    with pytest.raises(RuntimeError, match="pin"):
        archive.archive_tag_matches(str(remote), "archive", tip)
    bundle = archive.prepare_bundle(work, plan, inv, tmp_path / "bundles")
    bundle.write_bytes(bundle.read_bytes()[:80])
    with pytest.raises(subprocess.CalledProcessError):
        archive.reconstruct(remote.as_uri(), plan, inv, bundle, tmp_path / "bad-recovery")


def test_release_download_recovery_precedes_cleanup_and_supports_rerun(
    tmp_path, state, monkeypatch
):
    """Use real Git for all reads/writes and a directory-backed fixture for GitHub Releases."""
    import gzip
    import json
    import shutil
    from types import SimpleNamespace

    _, remote, inv, plan, meta, _, _, _, _ = state
    release_dir = tmp_path / "release"
    review = tmp_path / "approved"
    review.mkdir()
    inventory_file = review / archive.INVENTORY
    inventory_file.write_bytes(gzip.compress(archive.wire(inv), mtime=0))
    plan_file = review / "cleanup-plan.json"
    plan_file.write_bytes(archive.wire(plan))
    args = SimpleNamespace(
        inventory=inventory_file, plan=plan_file, receipt=review / "receipt.json"
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", archive.REPO)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    expected_url = "https://github.com/" + archive.REPO
    native_git, native_call = archive.git, archive.call
    corrupt_download = True

    def local_git(*args, **kwargs):
        if args[:3] == ("remote", "get-url", "origin"):
            return expected_url.encode()
        if args[0] in ("ls-remote", "push", "fetch"):
            args = [remote.as_uri() if part in ("origin", expected_url) else part for part in args]
        return native_git(*args, **kwargs)

    def live_metadata(repo):
        assert repo == archive.REPO
        data = copy.deepcopy(meta)
        data["branches"] = {
            n: {**data["branches"][n], "oid": oid}
            for n, oid in archive.git_refs(str(remote)).items()
        }
        return data

    def fixture_call(*args, **kwargs):
        if args[0] != "gh":
            return native_call(*args, **kwargs)
        action, tag = args[2:4]
        if action == "view":
            return subprocess.CompletedProcess(
                args,
                int(not release_dir.exists()),
                stdout=json.dumps({"tagName": tag, "isDraft": False}).encode(),
            )
        if action == "create":
            release_dir.mkdir()
            native_git("update-ref", "refs/tags/" + tag, plan["baseline"], cwd=remote)
            for item in args[-4:]:
                shutil.copy2(item, release_dir)
        elif action == "download":
            target = Path(args[-1])
            for item in release_dir.iterdir():
                shutil.copy2(item, target)
            if corrupt_download:
                (target / archive.BUNDLE).write_bytes(b"truncated release download")
        else:
            pytest.fail("Unexpected GitHub command: " + action)
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    monkeypatch.setattr(archive, "git", local_git)
    monkeypatch.setattr(archive, "call", fixture_call)
    monkeypatch.setattr(archive, "metadata", live_metadata)
    monkeypatch.setattr(archive, "gh_pages", lambda *_: [])
    original = archive.git_refs(str(remote))
    with pytest.raises(RuntimeError, match="Downloaded archive differs"):
        archive.cleanup(args)
    assert archive.git_refs(str(remote)) == original
    corrupt_download = False
    archive.cleanup(args)
    receipt = archive.read(args.receipt)
    assert receipt["state"] == "VERIFIED"
    assert receipt["open_prs_before"] == receipt["open_prs_after"] == meta["pulls"]
    assert receipt["before"] == original
    assert receipt["after"] == plan["retained"]
    assert receipt["deleted"] == plan["delete_after_verified_restore"]
    archive.cleanup(args)
    assert archive.read(args.receipt)["deleted"] == {}
    assert archive.git_refs(str(remote)) == plan["retained"]
