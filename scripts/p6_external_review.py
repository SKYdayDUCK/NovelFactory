"""Enroll two development-platform judges, hand off blind text and import signed audits."""

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.exc import SQLAlchemyError

from novel_factory.application import book_review, external_book_review
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import require
from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.domain.canonical import canonical_bytes, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.native_files import private_export
from novel_factory.infrastructure.private_files import read_private_bytes
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config


def _read(path):
    return strict_json_loads(read_private_bytes(path, 128 * 1024 * 1024))


def _write(path, data):
    with private_export(path) as stream:
        stream.write(canonical_bytes(data))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen", help="In each independent session generate its own key")
    keygen.add_argument("--key", required=True, type=Path, help="New owner-private key file")
    sign = commands.add_parser(
        "sign", help="Sign one completed audit offline in its assigned session"
    )
    sign.add_argument("--key", required=True, type=Path)
    sign.add_argument(
        "--assignment",
        required=True,
        type=Path,
        nargs="+",
        help="Its full export, or all one-packet exports in order",
    )
    sign.add_argument(
        "--assessment", required=True, type=Path, help="JSON with observations and report"
    )
    sign.add_argument("--output", required=True, type=Path)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--settings", required=True, type=Path)
    export = commands.add_parser("export")
    export.add_argument("--track", type=int, choices=(1, 2), required=True)
    export.add_argument("--packet", type=int, help="Export just this one-based packet")
    export.add_argument("--output", type=Path, required=True)
    submit = commands.add_parser("import")
    submit.add_argument("--audit", type=Path, required=True)
    status = commands.add_parser("status")
    status.add_argument("--reports", action="store_true")
    for command in (enroll, export, submit, status):
        command.add_argument(
            "--binding", type=Path, required=True, help="Private administrator project binding"
        )
    args = parser.parse_args(argv)
    if args.command == "keygen":
        key = ec.generate_private_key(ec.SECP256R1())
        with private_export(args.key) as stream:
            stream.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        return {
            "public_key": key.public_key()
            .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
            .hex()
        }
    if args.command == "sign":
        key = serialization.load_pem_private_key(read_private_bytes(args.key, 1024), password=None)
        require(
            isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1),
            "INVALID_ARGUMENT",
            "Expected an enrolled P-256 key",
        )
        packages = [_read(path) for path in args.assignment]
        assessment = _read(args.assessment)
        require(
            isinstance(assessment, dict) and set(assessment) == {"observations", "report"},
            "INVALID_ARGUMENT",
            "Expected one complete assessment",
        )
        binding = external_book_review.check_export(packages, assessment)
        payload = external_book_review.signed_bytes(binding, **assessment)
        _write(
            args.output,
            {
                "binding": binding,
                **assessment,
                "signature": key.sign(payload, ec.ECDSA(hashes.SHA256())).hex(),
            },
        )
        return {"assignment_id": binding["assignment_id"], "status": "SIGNED"}

    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        require(
            isinstance(runtime.binding, ProjectBinding),
            "VIEW_DENIED",
            "A project binding is required",
        )
        kernel, owner = runtime.kernel, runtime.principal
        scope = (kernel, owner, runtime.binding.work_id, runtime.binding.branch_id)
        if args.command == "enroll":
            settings = _read(args.settings)
            require(
                isinstance(settings, dict)
                and "external_judges" in settings
                and settings["external_judges"] is not None
                and set(settings)
                <= {
                    "release_request",
                    "external_judges",
                    "deadline_seconds",
                    "idempotency_key",
                    "revision_note",
                    "revision_evidence",
                },
                "INVALID_ARGUMENT",
                "Expected an external judge registration",
            )
            if "release_request" not in settings:
                from novel_factory.application.rolling_publication import final_review_request

                settings["release_request"] = final_review_request(*scope)
            wr = settings["release_request"]["world_ref"]
            require(
                (wr["work_id"], wr["branch_id"]) == scope[2:], "VIEW_DENIED", "Different project"
            )
            return book_review.start_review(kernel, AgentGateway(kernel, {}), owner, **settings)
        if args.command == "export":
            result = external_book_review.export_assignment(
                *scope, track=args.track, packet=args.packet
            )
            _write(args.output, result)
            return {"binding": result["binding"], "packets_exported": len(result["packets"])}
        if args.command == "import":
            return external_book_review.import_audit(*scope, _read(args.audit))
        return book_review.review_status(*scope, include_reports=args.reports)
    finally:
        runtime.kernel.db.close()


if __name__ == "__main__":
    try:
        receipt = main()
        print(json.dumps(receipt, ensure_ascii=False))
        if receipt.get("status") == "REJECTED":
            sys.exit(1)
    except DomainError as error:
        sys.exit(error.code)
    except FileExistsError:
        sys.exit("OUTPUT_EXISTS")
    except (KeyError, TypeError, ValueError, OSError):
        sys.exit("INVALID_INPUT_OR_OUTPUT")
    except SQLAlchemyError:
        sys.exit("SERVICE_UNAVAILABLE")
