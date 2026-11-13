"""Export the exact UTF-8 prose of a frozen NF release using a project binding."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.release import ReleaseService
from novel_factory.domain.canonical import strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.native_files import private_export
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--release-ref", type=Path, required=True, help="JSON ArtifactRef from the freeze receipt")
    parser.add_argument("--output", type=Path, required=True, help="new prose file in an existing directory")
    parser.add_argument("--reviewed-book", action="store_true", help="Export every released volume of the assessed P6 edition")
    args = parser.parse_args()
    with args.release_ref.open("rb") as stream:
        raw = stream.read(1025)
    if len(raw) > 1024:
        parser.error("release reference exceeds 1024 bytes")
    ref = strict_json_loads(raw)
    runtime = runtime_from_config(read_private_config(args.binding))
    try:
        binding = runtime.binding
        if not isinstance(binding, ProjectBinding):
            raise DomainError("VIEW_DENIED", "A project binding is required")
        releases = ReleaseService(runtime.kernel)
        frozen = releases.get_release(runtime.principal, binding.work_id, ref)
        if frozen["manifest"]["world_ref"]["branch_id"] != binding.branch_id:
            raise DomainError("VIEW_DENIED", "Release is outside the bound branch")
        if args.reviewed_book:
            from novel_factory.application.book_review import export_reviewed_book
            result = export_reviewed_book(runtime.kernel, runtime.principal, binding.work_id, binding.branch_id, ref)
        else:
            result = releases.export_release(runtime.principal, binding.work_id, ref)
    finally:
        runtime.kernel.db.close()

    data = result["text"].encode("utf-8")
    # Publish complete bytes at a new name; a retry preserves any prior export.
    with private_export(args.output) as pending:
        pending.write(data)
    print(json.dumps({
        **({"round_ref": result["round_ref"], "score_refs": result["score_refs"], "chapters": result["chapters"],
            "scenes": result["scenes"]} if args.reviewed_book else {}),
        "release_ref": result["release_ref"],
        "manifest_sha256": frozen["manifest"]["manifest_sha256"],
        "text_sha256": sha256(data).hexdigest(),
        "bytes": len(data),
        "scene_count": len(result["scenes"]),
        "external_delivery": "NOT_REQUESTED",
    }))


if __name__ == "__main__":
    try:
        main()
    except DomainError as error:
        sys.exit(error.code)
    except FileExistsError:
        sys.exit("OUTPUT_EXISTS")
    except (OSError, ValueError):
        sys.exit("INVALID_INPUT_OR_OUTPUT")
    except Exception:  # Database drivers may include private connection information.
        sys.exit("SERVICE_UNAVAILABLE")
