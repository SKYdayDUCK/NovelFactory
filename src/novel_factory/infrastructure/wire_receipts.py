"""Private, attempt-addressed transport receipts written before the HTTP worker returns."""

import base64
import hashlib
import json
from uuid import UUID

from .private_files import prepare_private_directory, read_private_bytes


def receipt_directory(store):
    return store.root / "http-receipts"


def destination(store, attempt):
    root = receipt_directory(store)
    prepare_private_directory(root)
    return str(root / (str(UUID(str(attempt))) + ".json"))


def read_receipt(store, attempt, request_hash, *, headers_only=False):
    path = receipt_directory(store) / (str(UUID(str(attempt))) + ".json")
    if headers_only:
        path = path.with_suffix(".headers.json")
    try:
        data = read_private_bytes(path, 3_000_000)
    except FileNotFoundError:
        return None
    receipt = json.loads(data)
    content = base64.b64decode(receipt["content"], validate=True)
    if (
        receipt["version"] != ("nf.http-headers.v1" if headers_only else "nf.http-receipt.v1")
        or (headers_only and (receipt["complete"] is not False or content))
        or receipt["attempt_id"] != str(attempt)
        or receipt["request_sha256"] != request_hash
        or len(content) > 2_000_000
        or hashlib.sha256(content).hexdigest() != receipt["body_sha256"]
    ):
        raise ValueError("Invalid recovered transport receipt")
    return receipt, content
