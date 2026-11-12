"""Post one reviewed supplier invoice line against its original, returned UNKNOWN attempt."""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import UUID

from novel_factory.application.mcp_runtime import ProjectBinding
from novel_factory.application.provider_evidence import reconcile_invoice
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.mcp_stdio import read_private_config, runtime_from_config


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binding", required=True, type=Path)
    p.add_argument("--invoice", required=True, type=Path, help="Private original invoice file")
    p.add_argument("--attempt", required=True, type=UUID)
    p.add_argument("--receipt-id", required=True)
    p.add_argument("--currency", required=True)
    p.add_argument("--amount", required=True, help="Reviewed actual amount of this single request")
    p.add_argument("--tokens", type=int, required=True)
    args = p.parse_args()
    with args.invoice.open("rb") as f:
        if f.seek(0, 2) == 0:
            p.error("Invoice file is empty")
        f.seek(0)
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    rt = runtime_from_config(read_private_config(args.binding))
    try:
        if not isinstance(rt.binding, ProjectBinding):
            p.error("A project binding is required")
        result = reconcile_invoice(
            rt.kernel,
            rt.principal,
            rt.binding.work_id,
            rt.binding.branch_id,
            str(args.attempt),
            receipt_id=args.receipt_id,
            currency=args.currency,
            amount=args.amount,
            tokens=args.tokens,
            document_sha256=digest,
        )
        print(json.dumps(result))
    finally:
        rt.kernel.db.close()


if __name__ == "__main__":
    try:
        main()
    except DomainError as exc:
        raise SystemExit(exc.code) from None
