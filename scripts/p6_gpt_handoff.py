"""Claim the native pair of frozen-edition tickets and collect independently signed GPT reads."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bootstrap_p5_m0 import save
from p6_gpt_reader import read
from p6_external_review import main as review_cli
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application import book_review
from novel_factory.application.kernel import require
from novel_factory.application.release import ReleaseService
from novel_factory.application.rolling_publication import final_review_request, queue_final_readers
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.native_files import file_lock
from novel_factory.infrastructure.private_files import prepare_private_directory


def run_reader(folder, operation):
    env = {k: v for k, v in os.environ.items() if k in {
        "HOME", "CODEX_HOME", "PATH", "PYTHONPATH", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "LD_LIBRARY_PATH", "TMPDIR"}}
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("p6_gpt_reader.py")), operation, str(folder)],
                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    require(result.returncode == 0, "GPT_READER_RECOVERY_REQUIRED", "Inspect the original reader session before retry")



def handle(kernel, owner, project, work, branch):
    scope = kernel, owner, work, branch
    queued = queue_final_readers(*scope)
    edition = queued["edition_sha256"]
    root = project / "external-reviews" / edition
    prepare_private_directory(root)
    with file_lock(root / "dispatch.lock"):
        tracks = [root / ticket for ticket in queued["reader_tickets"]]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run_reader, tracks, ["register", "register"]))
            judges = [read(folder / "identity.json") for folder in tracks]
            release = final_review_request(*scope)
            require(fingerprint(release) == edition, "STALE_GENERATION", "Frozen review edition changed")
            book_review.start_review(kernel, AgentGateway(kernel, {}), owner,
                release_request=release, external_judges=judges, deadline_seconds=86400,
                idempotency_key="p6-gpt-readers:" + edition)
            for track, folder in enumerate(tracks, 1):
                if not (folder / "assignment.json").exists():
                    review_cli(["export", "--binding", str(project / "control.json"), "--track", str(track),
                                "--output", str(folder / "assignment.json")])
            list(pool.map(run_reader, tracks, ["read", "read"]))
        for folder in tracks:
            receipt = review_cli(["import", "--binding", str(project / "control.json"), "--audit", str(folder / "signed.json")])
            require(receipt["status"] == "ACCEPTED", "BOOK_REVIEW_BLOCKED", "Signed review failed validation")
        state = book_review.review_status(*scope)
        require(state["state"] == "ASSESSED", "BOOK_REVIEW_BLOCKED", "Two full reads are required")
        result = {"review_round_ref": state["round_ref"], "result": state["result"], **queued}
        if state["result"]["status"] == "P6_QUALITY_NOT_MET":
            save(root, "returned.json", result)
            return {"status": "EXTERNAL_REVISION_REQUIRED", **result}
        frozen = ReleaseService(kernel).freeze(owner, release, "p6-final:" + edition)
        save(root, "accepted.json", {**result, "release_ref": frozen["release_ref"]})
        return {"status": "EXTERNAL_REVIEWS_ACCEPTED", **result, "release_ref": frozen["release_ref"]}
