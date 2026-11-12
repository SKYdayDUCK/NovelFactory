"""Offline enrollment for NF review and final editorial independence.

The registrar verifies each source subject-group record against its identity
provider before enrollment. Its private JSON files contain kind
VERIFIED_SUBJECT_GROUP, work_id, branch_id, principal_id, subject_group,
verified_by and source_record_id. The signing key and enrolled directory are
selected by NF_REVIEW_NATIVE_KEY and NF_REVIEW_NATIVE_ATTESTATIONS. Use a
private random key of at least 32 bytes, a mode-0700 directory and mode-0600
source files. Keep these outside role sandboxes. The case records the digest
of each input and expires at the operator-selected timestamp.

Run describe with a private creative-controller binding and the NF manuscript
and reader assignment. Run enroll with one --subject-evidence path per frozen
principal and --expires-at. Issue returns the review's ArtifactRef. Withdraw
removes the case immediately for subsequent verification. Workshop assignments
use their existing W4 reading evidence through NF_REVIEW_WORKSHOP_BINDING.

For a final decision, run describe/enroll/withdraw with --editor and the
actual editor's private controller binding, once per manuscript in the review.
Independently verify the exact NF grants and their subjects, including the
editor, against the identity provider. For Workshop reviews use W4's group
namespace and verify the delegated authors and reviewer against the W4 roster.
Supply any additional W4 producer groups as private subject-evidence records
of kind VERIFIED_WORKSHOP_PRODUCER_GROUP with work_id, branch_id, org_id,
manuscript_ref, subject_group, verified_by and source_record_id. The live
reading's group commitment must match the complete roster at decision time.
"""
import argparse
import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from novel_factory.application.mcp_runtime import CreativeControllerBinding
from novel_factory.application.review_independence import (
    ReviewIndependenceService, _check, _private_bytes, native_case_path, native_signature, validate_native_case,
)
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def enroll(binding, evidence_paths, expires_at):
    """Read isolated identity-provider records; callers of the NF controller cannot supply identity assertions."""
    workshop_editor = binding["authority"] == "WORKSHOP" and "publisher" in binding
    _check(binding["authority"] == "NATIVE" or workshop_editor)
    subjects, workshop_producers = {}, []
    for path in evidence_paths:
        raw = _private_bytes(path)
        evidence = json.loads(raw)
        _check(evidence["work_id"] == binding["world_ref"]["work_id"] and evidence["branch_id"] == binding["world_ref"]["branch_id"]
               and evidence["verified_by"].strip() and evidence["source_record_id"].strip())
        record = {"group": evidence["subject_group"], "evidence_sha256": sha256(raw).hexdigest()}
        if evidence["kind"] == "VERIFIED_WORKSHOP_PRODUCER_GROUP":
            _check(workshop_editor and evidence["org_id"] == binding["reviewer"]["delegation"]["org_id"]
                   and evidence["manuscript_ref"] == binding["manuscript_ref"])
            workshop_producers.append(record)
        else:
            _check(evidence["kind"] == "VERIFIED_SUBJECT_GROUP" and evidence["principal_id"] not in subjects)
            subjects[evidence["principal_id"]] = record
    case = {"version": "nf.native-review-case.v1", "binding": binding, "subjects": subjects, "expires_at": expires_at}
    if workshop_producers:
        case["workshop_producers"] = workshop_producers
    validate_native_case(binding, case)
    envelope = {"case": case, "signature": native_signature(case)}
    path = native_case_path(binding)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(envelope, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe", "enroll", "withdraw", "issue"))
    parser.add_argument("--editor", action="store_true", help="Bind the actual final editor for this manuscript")
    parser.add_argument("--controller-binding", required=True)
    parser.add_argument("--manuscript-ref", required=True, type=json.loads)
    parser.add_argument("--reviewer-task", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--subject-evidence", action="append", default=[], help="Private verified identity-provider record")
    parser.add_argument("--expires-at", help="Timezone-qualified ISO timestamp")
    args = parser.parse_args()
    runtime = runtime_from_config(read_private_config(args.controller_binding))
    _check(isinstance(runtime.binding, CreativeControllerBinding))
    service = ReviewIndependenceService(runtime.kernel)
    request = {"manuscript_ref": args.manuscript_ref, "reviewer_task_id": args.reviewer_task,
               "reviewer_generation": args.generation}
    _check(not (args.editor and args.action == "issue"))
    binding = service.describe(runtime.principal, for_editor=args.editor, **request)
    _check((runtime.binding.work_id, runtime.binding.branch_id) ==
           (binding["world_ref"]["work_id"], binding["world_ref"]["branch_id"]))
    if args.action == "enroll":
        _check(args.expires_at and args.subject_evidence)
        print(enroll(binding, args.subject_evidence, args.expires_at).name)
    elif args.action == "withdraw":
        _check(binding["authority"] == "NATIVE" or args.editor)
        native_case_path(binding).unlink(missing_ok=True)
    elif args.action == "issue":
        print(json.dumps(service.issue(runtime.principal, **request)))
    else:
        print(json.dumps(binding))


if __name__ == "__main__":
    main()
