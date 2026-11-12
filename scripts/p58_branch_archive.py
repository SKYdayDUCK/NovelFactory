"""Audit historical refs; replay a reviewed, exact-OID cleanup after verified release backup."""

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

REPO = "SKYdayDUCK/NovelFactory"
PRESERVE = {
    "main": "正式主线",
    "dev/p58-agent-runtime": "P5.8 开发和分阶段签收",
    "dev/p57-creative-harness-s1-s6": "P5.7 主线签收和独立审查原件",
    "dev/p3-p4-continuity": "开放 Scion PR #4 的原 base",
    "experiment/scion-incremental-20261111": "开放 Scion PR #4 的原 head 和旧基线证据",
    "experiment/scion-p58-compat": "已迁移至 P5.8 基线的独立 Scion 实验",
}
INVENTORY = "branch-inventory.json.gz"
BUNDLE = "p58-history.bundle"
RECEIPT = "p58-archive-receipt.json"
OID = re.compile(r"^[0-9a-f]{40}$")


def call(*args, cwd=None, check=True, data=None):
    return subprocess.run(
        [str(a) for a in args], cwd=cwd, input=data, check=check, stdout=subprocess.PIPE
    )


def git(*args, cwd=None, data=None):
    return call("git", *args, cwd=cwd, data=data).stdout


def read(path):
    return json.loads(Path(path).read_bytes())


def wire(data):
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def read_inventory(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def check(ok, message):
    if not ok:
        raise RuntimeError(message)


def gh_pages(repo, resource, field=None):
    response = call("gh", "api", "--paginate", "--slurp", f"repos/{repo}/{resource}").stdout
    pages = json.loads(response)
    if field:
        pages = [page[field] for page in pages]
    check(
        isinstance(pages, list) and all(isinstance(p, list) for p in pages),
        "Incomplete GitHub metadata",
    )
    return [item for page in pages for item in page]


def branch_mentions(body, names):
    text = (body or "").replace("/tree/", " ").replace("/blob/", " ").replace("/raw/", " ")
    text = text.replace("refs/heads/", " ").replace("origin/", " ")
    return [
        name
        for name in names
        if name != "main" and re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w.-])", text)
    ]


def discussion_refs(repo, names):
    hits = {}
    active = gh_pages(repo, "issues?state=open&per_page=100")
    for item in active:
        texts = [item]
        if item["comments"]:
            texts.extend(gh_pages(repo, f"issues/{item['number']}/comments?per_page=100"))
        if "pull_request" in item:
            texts.extend(gh_pages(repo, f"pulls/{item['number']}/comments?per_page=100"))
            texts.extend(gh_pages(repo, f"pulls/{item['number']}/reviews?per_page=100"))
        for text in texts:
            for name in branch_mentions(text.get("body"), names):
                hits.setdefault(name, []).append(text.get("html_url") or item["html_url"])
    return {n: sorted(set(urls)) for n, urls in hits.items()}


def metadata(repo):
    branches = gh_pages(repo, "branches?per_page=100")
    prs = gh_pages(repo, "pulls?state=open&per_page=100")
    runs = {}
    for status in ("queued", "in_progress", "waiting", "requested", "pending"):
        for run in gh_pages(
            repo, f"actions/runs?status={status}&per_page=100", field="workflow_runs"
        ):
            if (run.get("head_repository") or {}).get("full_name") == repo:
                runs[run["id"]] = {
                    "id": run["id"],
                    "branch": run["head_branch"],
                    "oid": run["head_sha"],
                }
    return {
        "repo": repo,
        "discussion_references": discussion_refs(repo, [b["name"] for b in branches]),
        "workflow_runs": list(runs.values()),
        "branches": {
            b["name"]: {"oid": b["commit"]["sha"], "protected": b["protected"]} for b in branches
        },
        "pulls": [
            {
                "number": pr["number"],
                **{
                    k: {
                        "branch": pr[k]["ref"],
                        "oid": pr[k]["sha"],
                        "repo": (pr[k]["repo"] or {}).get("full_name"),
                    }
                    for k in ("base", "head")
                },
            }
            for pr in prs
        ],
    }


def fixed_reasons(meta):
    groups = {n: [] for n in meta["branches"]}
    for name, info in meta["branches"].items():
        if name in PRESERVE:
            groups[name].append(PRESERVE[name])
        if info["protected"] is not False:
            groups[name].append("protected or protection unknown")
    for name, urls in meta["discussion_references"].items():
        if name in groups:
            groups[name].extend("open issue, PR or review reference: " + url for url in urls)
    for run in meta["workflow_runs"]:
        if run["branch"] in groups:
            groups[run["branch"]].append(f"active Actions run #{run['id']}")
    for pr in meta["pulls"]:
        for side in ("base", "head"):
            ref = pr[side]
            if ref["repo"] == meta["repo"] and ref["branch"] in groups:
                groups[ref["branch"]].append(f"open PR #{pr['number']} {side}")
    return groups


def git_refs(base):
    return {
        ref.removeprefix("refs/heads/"): oid
        for oid, ref in (
            line.split("\t", 1) for line in git("ls-remote", "--heads", base).decode().splitlines()
        )
    }


def valid_ref(name):
    return (
        isinstance(name, str)
        and not name.startswith("-")
        and call("git", "check-ref-format", "refs/heads/" + name, check=False).returncode == 0
    )


def references(base, names):
    """Scan tracked text, including handoff records. Report raw paths and matching line numbers."""
    names = [name for name in names if name != "main"]
    result = {name: {} for name in names}
    evidence_links = {name: {} for name in names}
    if not names:
        return result, evidence_links
    matches = re.compile(
        rb"(?<![a-zA-Z0-9._/-])("
        + b"|".join(re.escape(s.encode()) for s in sorted(names, key=len, reverse=True))
        + rb")(?![a-zA-Z0-9._-])"
    )
    # git grep has no line truncation here; read only files containing at least one full name.
    found = call(
        "git",
        "grep",
        "-I",
        "-l",
        "-z",
        "-F",
        "-f",
        "-",
        base,
        "--",
        ".",
        ":(exclude)reports/branch_archive/p58/**",
        data=("\n".join(names) + "\n").encode(),
        check=False,
    )
    check(found.returncode in (0, 1), "Cannot enumerate repository dependencies")
    for obj in found.stdout.split(b"\x00"):
        if not obj:
            continue
        _, path = obj.decode("utf8", "surrogateescape").split(":", 1)
        for number, line in enumerate(git("show", obj.decode()).splitlines(), 1):
            # In owner/repo/tree/<branch> there is a slash immediately before the name.
            normalized = (
                line.replace(b"raw.githubusercontent.com/" + REPO.encode() + b"/", b" ")
                .replace(b"/raw/", b" ")
                .replace(b"/tree/", b" ")
                .replace(b"/blob/", b" ")
                .replace(b"refs/heads/", b" ")
                .replace(b"refs/remotes/origin/", b" ")
                .replace(b"origin/", b" ")
            )
            for hit in set(matches.findall(normalized)):
                result[hit.decode()].setdefault(path, []).append(number)
                target = (
                    rb"(?:github\.com/"
                    + REPO.encode()
                    + rb"/(?:blob|raw|tree)/|raw\.githubusercontent\.com/"
                    + REPO.encode()
                    + rb"/)"
                )
                if re.search(
                    target + re.escape(hit) + rb"(?![a-zA-Z0-9._-])",
                    line,
                    re.IGNORECASE,
                ):
                    evidence_links[hit.decode()].setdefault(path, []).append(number)

    # Branch-specific CI patterns also identify live workflow dependencies. Exact
    # names above cover YAML without globs; nested/inline YAML lists are both read.
    patterns = re.compile(rb"""(?:[a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_./*-]*[*?][a-zA-Z0-9_./*?-]*""")
    flows = git("ls-tree", "-r", "-z", "--name-only", base, "--", ".github/workflows").split(b"\0")
    for item in filter(None, flows):
        path = item.decode()
        if not path.endswith((".yml", ".yaml")):
            continue
        block_indent = None
        for number, line in enumerate(git("show", base + ":" + path).splitlines(), 1):
            if not line.strip() or line.lstrip().startswith(b"#"):
                continue
            indent = len(line) - len(line.lstrip())
            begins = re.search(rb"\bbranches\s*:", line)
            if begins:
                block_indent = indent
            elif (
                block_indent is None or indent <= block_indent or not line.lstrip().startswith(b"-")
            ):
                block_indent = None
                continue
            for pattern in patterns.findall(line.split(b"#", 1)[0]):
                for name in names:
                    if fnmatch.fnmatchcase(name, pattern.decode()):
                        hits = result[name].setdefault(path, [])
                        if number not in hits:
                            hits.append(number)
    return result, evidence_links


def runtime_ref(path):
    return path.startswith(
        (
            ".github/",
            ".agents/",
            "scripts/",
            "adapters/",
            "workshop/scripts/",
            "workshop/adapters/",
            "workshop/qualification/",
            "workshop/benchmarks/",
            "workshop/tests/",
            "src/",
            "runtime/",
            "agent_runtime/",
            "experiments/",
            "configs/",
            "tests/",
            "benchmarks/",
            "validation/",
        )
    ) or path in {"README.md", "AGENTS.md", "reports/integration/CURRENT_STAGE.md"}


def historical_blobs(base, tip):
    # rev-list reports one representative path for each object in the branch-only history.
    # Preserve even files deleted before the tip; the complete commit trees remain in the bundle.
    names = {}
    for line in git(
        "-c", "core.quotepath=false", "rev-list", "--objects", tip, "^" + base
    ).splitlines():
        oid, separator, name = line.partition(b" ")
        if separator and name:
            names[oid] = name.decode()
    listing = git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        data=b"".join(oid + b"\n" for oid in names),
    )
    return [
        {"oid": oid.decode(), "historical_path": names[oid], "bytes": int(size)}
        for oid, kind, size in [line.split() for line in listing.splitlines()]
        if kind == b"blob"
    ]


def describe(base, tip, main_tree, main_objects):
    result = call("git", "merge-base", "--all", base, tip, check=False)
    check(result.returncode in (0, 1), f"Merge-base discovery failed at {tip}")
    mb = sorted(result.stdout.decode().splitlines())
    comparison = (
        mb[0]
        if mb
        else git("hash-object", "-w", "-t", "tree", "--stdin", data=b"").decode().strip()
    )
    behind, ahead = map(int, git("rev-list", "--left-right", "--count", base + "..." + tip).split())
    changes = []
    if ahead:
        chunks = git("diff", "--no-renames", "--raw", "--no-abbrev", "-z", comparison, tip).split(
            b"\0"
        )
        for i in range(0, len(chunks) - 1, 2):
            _, _, old, new, status = chunks[i].decode().split()
            path = chunks[i + 1].decode()
            changes.append(
                {
                    "path": path,
                    "change": status,
                    "base_oid": old,
                    "tip_oid": new,
                    "main_path_oid": main_tree.get(path),
                    "tip_oid_in_main_history": new in main_objects,
                }
            )
    return {
        "tip": tip,
        "merge_bases": mb,
        "path_diff_against": comparison,
        "ahead": ahead,
        "behind": behind,
        "independent_commits": git("rev-list", "--reverse", tip, "^" + base).decode().splitlines(),
        "changed_paths_since_merge_base": changes,
        "history_blobs_unreachable_from_main": historical_blobs(base, tip) if ahead else [],
    }


def inventory(meta):
    branches = meta["branches"]
    base = branches["main"]["oid"]
    check(
        meta["repo"] == REPO and OID.fullmatch(base),
        "Unexpected repository or missing baseline",
    )
    check(
        all(valid_ref(n) and OID.fullmatch(i["oid"]) for n, i in branches.items()),
        "Invalid refs",
    )
    main_tree = {
        path.decode(): header.split()[-1].decode()
        for line in git("ls-tree", "-r", "-z", base).split(b"\0")
        if line
        for header, path in [line.split(b"\t", 1)]
    }
    main_objects = set(
        git("rev-list", "--objects", "--no-object-names", base).decode().splitlines()
    )
    deps, evidence_links = references(base, branches)
    fixed = fixed_reasons(meta)
    rows = []
    for name in sorted(branches):
        entry = describe(base, branches[name]["oid"], main_tree, main_objects)
        links = deps.get(name, {})
        active = [p for p in links if runtime_ref(p) or p in evidence_links.get(name, {})]
        reasons = fixed[name] + (["operational reference: " + p for p in active])
        classification = "C" if reasons else "B" if entry["ahead"] else "A"
        rows.append(
            {
                "name": name,
                **entry,
                "class": classification,
                "retain_reasons": reasons,
                "references_in_tracked_text": links,
                "evidence_urls_by_tracked_path": evidence_links.get(name, {}),
            }
        )
    return {
        "version": 1,
        "repo": REPO,
        "baseline": base,
        "open_prs": meta["pulls"],
        "open_discussion_references": meta["discussion_references"],
        "active_workflow_runs": meta["workflow_runs"],
        "branches": rows,
    }


def make_plan(inv):
    return {
        "version": 1,
        "repo": REPO,
        "baseline": inv["baseline"],
        "inventory_sha256": sha(wire(inv)),
        "retained": {r["name"]: r["tip"] for r in inv["branches"] if r["class"] == "C"},
        "delete_after_verified_restore": {
            r["name"]: r["tip"] for r in inv["branches"] if r["class"] in ("A", "B")
        },
    }


def readable_inventory(inv):
    rows = inv["branches"]
    intro = [
        "# 逐分支审计索引",
        "",
        f"正式 main 前提为 `{inv['baseline']}`。共审计 {len(rows)} 个远端分支。逐对象清单和依赖位置位于同目录下的 `branch-inventory.json.gz`。执行时的实际前后分支数、精确原 SHA 和删除回执以归档 release 对应运行记录为准。",
        "",
        "| 分支与 tip | 相对 main 超前 / 落后 | 独有历史 blob | 类别 | 拟处理及理由 |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        reason = (
            "；".join(row["retain_reasons"])
            if row["class"] == "C"
            else (
                "main 已包含全部历史，恢复通过后清理"
                if row["class"] == "A"
                else "恢复独有提交和对象后清理"
            )
        )
        reason = reason.replace("|", r"\|")
        intro.append(
            f"| `{row['name']}` · `{row['tip']}` | {row['ahead']} / {row['behind']} | "
            f"{len(row['history_blobs_unreachable_from_main'])} | {row['class']} | {reason} |"
        )
    return "\n".join(intro) + "\n"


def reconstruct(remote, plan, inv, archive, folder, base_ref="refs/heads/main"):
    """Populate a fresh repo from the pinned main and the downloaded bundle only. No alternates."""
    folder = Path(folder)
    folder.mkdir(exist_ok=True, parents=True)
    check(not any(folder.iterdir()), "Recovery directory must be empty")
    dest = folder / "upstream.git"
    git("init", "--bare", "-b", "main", dest)
    git(
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        remote,
        base_ref + ":refs/p58-baseline/incoming",
        cwd=dest,
    )
    # Local network fetch may advance main beyond baseline. Trim to exactly the old baseline and
    # repack in a *second* empty repo so newer objects cannot mask an incomplete bundle.
    source = folder / "recovered.git"
    git("init", "--bare", "-b", "main", source)
    git(
        "fetch",
        "--no-tags",
        "--no-write-fetch-head",
        "file://" + str(dest.resolve()),
        plan["baseline"] + ":refs/heads/main",
        cwd=source,
    )
    git("bundle", "verify", archive, cwd=source)
    advertised = git("bundle", "list-heads", archive, cwd=source).decode().splitlines()
    by_name = {
        ref.removeprefix("refs/p58-archive/"): oid for oid, ref in [r.split() for r in advertised]
    }
    expected = {r["name"]: r["tip"] for r in inv["branches"]}
    check(
        all(expected.get(n) == oid for n, oid in by_name.items()),
        "Bundle advertised unexpected references",
    )
    if by_name:
        git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            archive,
            "refs/p58-archive/*:refs/p58-restore/*",
            cwd=source,
        )
    for name, tip in expected.items():
        git("cat-file", "-e", tip + "^{commit}", cwd=source)
        git("update-ref", "refs/p58-restore/" + name, tip, cwd=source)
    git("fsck", "--full", "--no-reflogs", cwd=source)
    return len(expected)


def prepare_bundle(repo, plan, inv, directory):
    directory.mkdir(exist_ok=True, parents=True)
    bundle = (directory / BUNDLE).resolve()
    with tempfile.TemporaryDirectory() as work:
        bare = Path(work) / "archive.git"
        git("init", "--bare", "-b", "main", bare)
        # A local shared clone would rely on someone else's reflog. Fetch exact objects first.
        refs = [r["tip"] + ":refs/p58-archive/" + r["name"] for r in inv["branches"]]
        git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "file://" + str(Path(repo).resolve()),
            *refs,
            cwd=bare,
        )
        selectors = ["refs/p58-archive/" + r["name"] for r in inv["branches"] if r["ahead"]]
        if selectors:
            git("bundle", "create", bundle, *selectors, "^" + plan["baseline"], cwd=bare)
        else:
            # The release remains self-verifying even for an all-ancestor inventory.
            git("bundle", "create", bundle, "refs/p58-archive/main", cwd=bare)
    return bundle


def validate(plan, inv):
    check(
        plan["repo"] == REPO
        and inv["repo"] == REPO
        and plan["version"] == inv["version"] == 1
        and plan["baseline"] == inv["baseline"]
        and plan["inventory_sha256"] == sha(wire(inv)),
        "Plan binding differs",
    )
    classified = make_plan(inv)
    deleted, kept = plan["delete_after_verified_restore"], plan["retained"]
    check(
        isinstance(deleted, dict)
        and isinstance(kept, dict)
        and not set(deleted).intersection(kept),
        "Invalid or overlapping plan entries",
    )
    check(
        {**deleted, **kept}
        == {**classified["retained"], **classified["delete_after_verified_restore"]},
        "Missing branch or changed reviewed tip",
    )
    check(
        all(
            classified["delete_after_verified_restore"].get(n) == oid for n, oid in deleted.items()
        ),
        "Cleanup exceeds the audited candidates",
    )
    check(
        PRESERVE.keys().isdisjoint(plan["delete_after_verified_restore"]),
        "Protected branch in cleanup",
    )


def final_gate(meta, plan, inv):
    validate(plan, inv)
    tips = meta["branches"]
    fixed = fixed_reasons(meta)
    original_scion = next((pr for pr in inv["open_prs"] if pr["number"] == 4), None)
    check(
        original_scion is None or original_scion in meta["pulls"],
        "Original Scion PR #4 changed during review",
    )
    base = tips["main"]["oid"]
    check(
        call("git", "merge-base", "--is-ancestor", plan["baseline"], base, check=False).returncode
        == 0,
        "Reviewed baseline is outside current main",
    )
    names = list(plan["delete_after_verified_restore"])
    active, evidence_links = references(base, names)
    for name, oid in plan["delete_after_verified_restore"].items():
        if name not in tips:
            continue  # previously deleted; never recreate
        check(
            tips[name]["oid"] == oid
            and not fixed[name]
            and not any(runtime_ref(p) for p in active[name])
            and not evidence_links[name],
            "Branch changed, protected, referenced or in an open PR: " + name,
        )
    return {n: oid for n, oid in plan["delete_after_verified_restore"].items() if n in tips}


def archive_tag_matches(remote, tag, baseline):
    name = "refs/tags/" + tag
    refs = {
        ref: oid
        for oid, ref in (
            line.split()
            for line in git("ls-remote", "--tags", remote, name, name + "^{}").decode().splitlines()
        )
    }
    check(
        refs.get(name + "^{}", refs.get(name)) == baseline,
        "Archive tag does not pin reviewed baseline",
    )


def delete_reviewed(remote, allowed):
    check(
        all(valid_ref(n) and OID.fullmatch(oid) for n, oid in allowed.items())
        and PRESERVE.keys().isdisjoint(allowed),
        "Invalid deletion target",
    )
    if allowed:
        git(
            "push",
            "--atomic",
            "--porcelain",
            *["--force-with-lease=refs/heads/" + n + ":" + oid for n, oid in allowed.items()],
            remote,
            *[":refs/heads/" + n for n in allowed],
        )


def cleanup(args):
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == REPO
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    ):
        raise RuntimeError("Run this write command from the reviewed main workflow")
    inv = read_inventory(args.inventory)
    plan = read(args.plan)
    validate(plan, inv)
    tag = "p58-history-" + plan["inventory_sha256"][:16]
    repo = Path.cwd()
    remote_url = git("remote", "get-url", "origin").decode().strip().removesuffix(".git")
    check(
        remote_url.lower() == ("https://github.com/" + REPO).lower(),
        "Unexpected origin",
    )
    live = metadata(REPO)
    final_gate(live, plan, inv)
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        release = call(
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            REPO,
            "--json",
            "tagName",
            check=False,
        )
        if release.returncode != 0:
            bundle = prepare_bundle(repo, plan, inv, work)
            restore_dir = work / "before"
            restore_dir.mkdir()
            number = reconstruct(
                git("remote", "get-url", "origin").decode().strip(),
                plan,
                inv,
                bundle,
                restore_dir,
            )
            evidence = {
                "baseline": plan["baseline"],
                "manifest_sha256": plan["inventory_sha256"],
                "bundle_sha256": sha(bundle.read_bytes()),
                "restored_ref_count": number,
            }
            (work / RECEIPT).write_bytes(wire(evidence))
            (work / INVENTORY).write_bytes(gzip.compress(wire(inv), mtime=0))
            call(
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                REPO,
                "--target",
                plan["baseline"],
                "--title",
                "P5.8 historical Git archive",
                "--notes",
                "Verified historical branch objects and recovery manifest.",
                "--latest=false",
                bundle,
                work / RECEIPT,
                work / INVENTORY,
                Path(args.plan).resolve(),
            )
        release = json.loads(
            call(
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                REPO,
                "--json",
                "tagName,isDraft",
            ).stdout
        )
        check(
            release["tagName"] == tag and release["isDraft"] is False,
            "Archive release is unpublished",
        )
        archive_tag_matches("origin", tag, plan["baseline"])
        downloaded = work / "downloaded"
        downloaded.mkdir()
        call("gh", "release", "download", tag, "--repo", REPO, "--dir", downloaded)
        evidence = read(downloaded / RECEIPT)
        check(
            read(downloaded / Path(args.plan).name) == plan
            and read_inventory(downloaded / INVENTORY) == inv
            and evidence["baseline"] == plan["baseline"]
            and evidence["manifest_sha256"] == plan["inventory_sha256"]
            and sha((downloaded / BUNDLE).read_bytes()) == evidence["bundle_sha256"],
            "Downloaded archive differs",
        )
        restore_dir = work / "after"
        restore_dir.mkdir()
        remote = git("remote", "get-url", "origin").decode().strip()
        count = reconstruct(remote, plan, inv, downloaded / BUNDLE, restore_dir)
        check(count == evidence["restored_ref_count"], "Downloaded restore count differs")
        latest = metadata(REPO)
        actual = git_refs("origin")
        check(
            actual == {n: i["oid"] for n, i in latest["branches"].items()},
            "Remote/API ref snapshot changed",
        )
        allowed = final_gate(latest, plan, inv)
        for name in allowed:
            effective = gh_pages(REPO, "rules/branches/" + quote(name, safe="") + "?per_page=100")
            check(not effective, "Effective branch rules present: " + name)
        # Refresh immediately before the single atomic ref transaction; leases enforce the
        # reviewed old OID on the server. These refspecs cannot update any branch contents.
        refreshed = metadata(REPO)
        check(allowed == final_gate(refreshed, plan, inv), "Review state changed during validation")
        actual = git_refs("origin")
        check(
            actual == {n: i["oid"] for n, i in refreshed["branches"].items()},
            "Ref snapshot changed immediately before deletion",
        )
        # Preserve a receipt even if the transport dies just after an accepted push or
        # a postflight API read fails. A runner may then publish its unconfirmed status.
        result = {
            "repo": REPO,
            "archive_tag": tag,
            "archive": evidence,
            "before": actual,
            "requested": allowed,
            "open_prs_before": refreshed["pulls"],
            "after": None,
            "deleted": None,
            "open_prs_after": None,
            "state": "UNCONFIRMED",
        }
        receipt = Path(args.receipt)
        receipt.write_bytes(wire(result))
        delete_reviewed("origin", allowed)
        after = git_refs("origin")
        result.update(
            after=after,
            deleted={n: oid for n, oid in allowed.items() if n not in after},
            unchosen_refs_that_moved={
                n: v for n, v in after.items() if n in actual and v != actual[n]
            },
        )
        receipt.write_bytes(wire(result))
        postflight = metadata(REPO)
        result["open_prs_after"] = postflight["pulls"]
        receipt.write_bytes(wire(result))
        check(not set(allowed).intersection(after), "Remote deletion not confirmed")
        check(
            set(actual).difference(allowed).issubset(after),
            "Unselected ref disappeared; inspect the postflight snapshot",
        )
        scion = next((p for p in refreshed["pulls"] if p["number"] == 4), None)
        check(scion is None or scion in postflight["pulls"], "Scion PR #4 changed during cleanup")
        result["state"] = "VERIFIED"
        receipt.write_bytes(wire(result))
        print(
            json.dumps(
                {
                    "archive_tag": tag,
                    "deleted_count": len(allowed),
                    "remaining": len(after),
                }
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmd = parser.add_subparsers(dest="cmd", required=True)
    i = cmd.add_parser("inventory")
    i.add_argument("--metadata")
    i.add_argument("--out", type=Path, required=True)
    for mode in ("rehearse", "restore", "apply"):
        p = cmd.add_parser(mode)
        p.add_argument("--inventory", required=True)
        p.add_argument("--plan", required=True)
        if mode in ("rehearse", "restore"):
            p.add_argument("--out", required=True, type=Path)
            p.add_argument("--remote", required=True)
            if mode == "restore":
                p.add_argument("--archive", required=True, type=Path)
                p.add_argument("--receipt", required=True)
                p.add_argument(
                    "--base-ref",
                    default="refs/heads/main",
                    help="Pinned archive tag (refs/tags/...) or a branch containing the baseline",
                )
        else:
            p.add_argument("--receipt", required=True)
    a = parser.parse_args()
    if a.cmd == "inventory":
        meta = read(a.metadata) if a.metadata else metadata(REPO)
        inv = inventory(meta)
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / INVENTORY).write_bytes(gzip.compress(wire(inv), mtime=0))
        (a.out / "cleanup-plan.json").write_bytes(wire(make_plan(inv)))
        (a.out / "BRANCHES.md").write_text(readable_inventory(inv), encoding="utf-8")
    elif a.cmd == "apply":
        cleanup(a)
    else:
        inv = read_inventory(a.inventory)
        plan = read(a.plan)
        validate(plan, inv)
        if a.cmd == "restore":
            receipt = read(a.receipt)
            check(
                receipt["baseline"] == plan["baseline"]
                and receipt.get("manifest_sha256", receipt.get("inventory_sha256"))
                == plan["inventory_sha256"]
                and receipt["bundle_sha256"] == sha(a.archive.read_bytes()),
                "Archive receipt mismatch",
            )
            check(
                any(a.base_ref.startswith(s) for s in ("refs/tags/", "refs/heads/"))
                and call("git", "check-ref-format", a.base_ref, check=False).returncode == 0,
                "Invalid baseline reference",
            )
            count = reconstruct(a.remote, plan, inv, a.archive.resolve(), a.out, a.base_ref)
            check(count == receipt["restored_ref_count"], "Restore count mismatch")
            print(json.dumps({"restored": count, "repository": str(a.out / "recovered.git")}))
            return
        bundle = prepare_bundle(Path.cwd(), plan, inv, a.out)
        with tempfile.TemporaryDirectory() as folder:
            count = reconstruct(a.remote, plan, inv, bundle, folder)
        receipt = {
            "baseline": plan["baseline"],
            "inventory_sha256": plan["inventory_sha256"],
            "bundle_sha256": sha(bundle.read_bytes()),
            "restored_ref_count": count,
            "remote_refs_deleted": 0,
        }
        (a.out / RECEIPT).write_bytes(wire(receipt))
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
