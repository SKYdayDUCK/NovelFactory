"""Offline regressions for the exact main checkout and dual engineering RC receipts."""
import subprocess
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from scripts.engineering_release_ci import WORKFLOWS, both_workflows_passed, nf_source


class SourceTests(unittest.TestCase):
    def test_only_integrated_source_at_event_sha_can_qualify_on_main(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
            sha = subprocess.check_output(["git", "-C", tmp, "rev-parse", "HEAD"], text=True).strip()
            for entry in ("scripts/p6_budget.py", "src/novel_factory/interfaces/delegation_stdio.py"):
                peer = root / "nf-peer"
                (peer / entry).parent.mkdir(parents=True)
                (peer / entry).touch()
                self.assertEqual(nf_source(root, entry, "pull_request", "refs/pull/16/merge", sha), peer)
                for event in ("push", "workflow_dispatch"):
                    with self.assertRaisesRegex(ValueError, "absent on main"):
                        nf_source(root, entry, event, "refs/heads/main", sha)
                (root / entry).parent.mkdir(parents=True)
                (root / entry).touch()
                with self.assertRaisesRegex(ValueError, "differs from the event commit"):
                    nf_source(root, entry, "push", "refs/heads/main", sha)
                subprocess.run(["git", "-C", tmp, "add", "--", entry], check=True)
                subprocess.run(["git", "-C", tmp, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                                "commit", "-q", "-m", "integrated entry"], check=True)
                sha = subprocess.check_output(["git", "-C", tmp, "rev-parse", "HEAD"], text=True).strip()
                self.assertEqual(nf_source(root, entry, "push", "refs/heads/main", sha), root)
                (root / entry).write_text("local replacement")
                with self.assertRaisesRegex(ValueError, "differs from the event commit"):
                    nf_source(root, entry, "push", "refs/heads/main", sha)
                with self.assertRaisesRegex(ValueError, "event SHA"):
                    nf_source(root, entry, "push", "refs/heads/main", "0" * 40)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.runs = {}
        self.current = {}
        for i, filename in enumerate(WORKFLOWS, 1):
            run = dict(id=i, run_number=10, run_attempt=1, workflow_id=100+i,
                       path=f".github/workflows/{filename}", head_branch="main", head_sha=self.sha,
                       event="push", status="completed", conclusion="success")
            self.runs[filename] = [run]
            self.current[i] = deepcopy(run)

    def get(self, endpoint):
        url = urlsplit(endpoint)
        parts = url.path.split("/")
        if parts[-1] == "runs":
            self.assertEqual(parse_qs(url.query), dict(branch=["main"], event=["push"], head_sha=[self.sha], per_page=["100"]))
            return {"workflow_runs": self.runs[parts[-2]]}
        return self.current[int(parts[-1])]

    def ready(self):
        return both_workflows_passed("test/nf", self.sha, self.get)

    def test_both_latest_main_pushes_succeeded(self):
        self.assertTrue(self.ready())

    def test_newer_run_preempts_earlier_success_even_when_listing_order_changes(self):
        for filename in WORKFLOWS:
            for status, conclusion in (("queued", None), ("completed", "failure"), ("completed", "cancelled")):
                with self.subTest(filename=filename, status=status, conclusion=conclusion):
                    first, = self.runs[filename]
                    newer = dict(first, id=45, run_number=11, status=status, conclusion=conclusion)
                    self.runs[filename] = [newer, first]
                    self.current[45] = newer
                    self.assertFalse(self.ready())
                    self.runs[filename] = [first]

    def test_run_attempt_changed_between_listing_and_check(self):
        for filename in WORKFLOWS:
            run, = self.runs[filename]
            self.current[run["id"]] = dict(run, run_attempt=2, status="in_progress", conclusion=None)
            self.assertFalse(self.ready())
            self.current[run["id"]] = dict(run, run_attempt=2)
            self.assertTrue(self.ready())

    def test_other_path_branch_event_sha_and_missing_check_cannot_qualify(self):
        for filename in WORKFLOWS:
            run, = self.runs[filename]
            for key, value in (("path", ".github/workflows/same-name.yml"), ("head_branch", "other"),
                               ("event", "pull_request"), ("head_sha", "b" * 40)):
                with self.subTest(filename=filename, key=key):
                    self.runs[filename] = [dict(run, **{key: value})]
                    self.assertFalse(self.ready())
            self.runs[filename] = []
            self.assertFalse(self.ready())
            self.runs[filename] = [run]


if __name__ == "__main__":
    unittest.main()
