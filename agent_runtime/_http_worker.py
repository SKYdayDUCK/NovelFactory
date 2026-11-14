"""Private, one-request stdio helper. Parent owns a hard wall-clock deadline."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _persist(options, status, content, complete, receipt, window, *, headers_only=False):
    """Publish only returned bytes/metadata. The parent may already be gone."""
    target = options.get("receipt_target")
    if target is None:
        return
    dest = Path(target["path"])
    payload = {"version": "nf.http-receipt.v1", "attempt_id": target["attempt_id"],
               "request_sha256": options["request_hash"], "http_status": status, "complete": complete,
               "body_sha256": hashlib.sha256(content).hexdigest(),
               "content": base64.b64encode(content).decode("ascii"),
               "receipt": receipt, "transport_window": window}
    if headers_only:
        dest = dest.with_suffix(".headers.json")
        payload["version"] = "nf.http-headers.v1"
    fd, name = tempfile.mkstemp(dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(payload, ensure_ascii=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, dest)
        if os.name != "nt":
            dfd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        os.unlink(name)


def main():
    try:
        options = json.load(sys.stdin)
        body = json.dumps(options["payload"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        request = Request(options["url"], data=body, method="POST", headers={
            "Authorization": "Bearer " + options["credential"], "Content-Type": "application/json",
            "X-Client-Request-Id": options["request_hash"], **(options.get("headers") or {})})
        context = None
        if urlsplit(options["url"]).scheme == "https":
            try:
                capath = os.environ.get("SSL_CERT_DIR")
                if capath and not Path(capath).is_dir():
                    raise ValueError("Invalid TLS trust directory")
                context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"), capath=capath)
            except (OSError, ValueError):
                json.dump({"failure": "MODEL_TLS_CONFIGURATION_INVALID", "sent": False}, sys.stdout)
                return
        transport = build_opener(_NoRedirect, HTTPSHandler(context=context))
        started_at = datetime.now(UTC).isoformat()
        try:
            reply = transport.open(request, timeout=options["timeout"])
        except HTTPError as err:
            reply = err
        with reply:
            limit = options["limit"]
            receipt = reply.headers.get("x-request-id")
            _persist(options, reply.status, b"", False, receipt,
                     {"started_at": started_at, "received_at": datetime.now(UTC).isoformat()}, headers_only=True)
            content = reply.read(limit + 1)
            size = reply.headers.get("Content-Length")
            complete = len(content) <= limit and (size is None or size.isdecimal() and int(size) == len(content))
            window = {"started_at": started_at, "received_at": datetime.now(UTC).isoformat()}
            _persist(options, reply.status, content[:limit], complete, receipt, window)
            if reply.status >= 300:
                result = {"failure": "MODEL_RATE_LIMITED" if reply.status == 429 else "MODEL_HTTP_ERROR",
                          "status": reply.status}
            elif len(content) > limit:
                result = {"failure": "MODEL_RESPONSE_TOO_LARGE"}
            elif not complete:
                result = {"failure": "MODEL_TRANSPORT_UNKNOWN"}
            else:
                result = {"content": base64.b64encode(content).decode("ascii"), "receipt": receipt,
                          "transport_window": window}
    except (TimeoutError, OSError, ValueError, KeyError, TypeError):
        result = {"failure": "MODEL_TRANSPORT_UNKNOWN"}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
