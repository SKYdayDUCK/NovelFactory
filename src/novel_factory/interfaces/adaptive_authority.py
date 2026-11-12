"""Provision or withdraw a signed, exact instruction-promotion case under independent custody.

The private subject-evidence format, signing key and case directory are shared
with the native review registrar. Each participating grant's principal is
covered. The reviewer and publisher must belong to separate groups outside
the optimizer and evaluator groups. A case is specific to one frozen candidate,
experiment, review assignment and publishing grant.
"""

import argparse
import json
import hmac
import os
import re
from pathlib import Path

from novel_factory.application.adaptive_promotion import AdaptivePromotionService
from novel_factory.application.mcp_runtime import CreativeControllerBinding
from novel_factory.application.review_independence import (
    _check,
    _private_bytes,
    native_case_path,
    native_signature,
)
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config
from novel_factory.interfaces.review_independence import enroll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("describe", "enroll", "withdraw"))
    parser.add_argument("--controller-binding", required=True)
    parser.add_argument(
        "--case-id", help="Withdraw an exact previously enrolled case, including after the review task ended"
    )
    for flag in ("candidate-ref", "engineering-report-ref", "run-manifest-ref"):
        parser.add_argument("--" + flag, type=json.loads)
    parser.add_argument("--reviewer-task-id")
    parser.add_argument("--reviewer-generation", type=int)
    parser.add_argument("--publisher-grant-id")
    parser.add_argument("--subject-evidence", action="append", default=[])
    parser.add_argument("--expires-at")
    args = vars(parser.parse_args())
    rt = runtime_from_config(read_private_config(args.pop("controller_binding")))
    _check(isinstance(rt.binding, CreativeControllerBinding))
    action, paths, expiry = args.pop("action"), args.pop("subject_evidence"), args.pop("expires_at")
    identifier = args.pop("case_id")
    svc = AdaptivePromotionService(rt.kernel)
    svc._enabled()
    if identifier is not None:
        _check(action == "withdraw" and re.fullmatch(r"[a-f0-9]{64}", identifier))
        path = Path(os.environ["NF_REVIEW_NATIVE_ATTESTATIONS"]) / (identifier + ".json")
        envelope = json.loads(_private_bytes(path))
        signed = envelope["case"]
        case = signed["binding"]
        _check(
            case.get("kind") == "INSTRUCTION_PROMOTION_CASE"
            and hmac.compare_digest(envelope["signature"], native_signature(signed))
            and native_case_path(case) == path
        )
        wr = case["world_ref"]
        _check((wr["work_id"], wr["branch_id"]) == (rt.binding.work_id, rt.binding.branch_id))
        with rt.kernel.db.uow(write=False) as repo:
            svc._control(repo, rt.principal, wr)
        path.unlink(missing_ok=True)
        return
    _check(all(value is not None for value in args.values()))
    with rt.kernel.db.uow(write=False) as repo:
        rt.kernel.authorize(repo, rt.principal, rt.binding.work_id, "ADMIN", rt.binding.branch_id)
        record = rt.kernel.artifact(
            repo, args["candidate_ref"], rt.binding.work_id, kind="CREATIVE_INSTRUCTION_CANDIDATE"
        )
        wr = record["payload"]["scope"]["world_ref"]
        _check(wr["branch_id"] == rt.binding.branch_id)
    case = svc.describe_case(rt.principal, wr, **args)
    if action == "enroll":
        _check(expiry and paths)
        print(enroll(case, paths, expiry).name)
    elif action == "withdraw":
        native_case_path(case).unlink(missing_ok=True)
    else:
        print(json.dumps(case))


if __name__ == "__main__":
    main()
