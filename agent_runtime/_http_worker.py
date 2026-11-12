"""Private, one-request stdio helper. Parent owns a hard wall-clock deadline."""
from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    try:
        options = json.load(sys.stdin)
        body = json.dumps(options["payload"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        request = Request(options["url"], data=body, method="POST", headers={
            "Authorization": "Bearer " + options["credential"], "Content-Type": "application/json",
            "X-Client-Request-Id": options["request_hash"], **(options.get("headers") or {})})
        transport = build_opener(_NoRedirect)
        started_at = datetime.now(UTC).isoformat()
        with transport.open(request, timeout=options["timeout"]) as reply:
            content = reply.read(options["limit"] + 1)
            received_at = datetime.now(UTC).isoformat()
            if len(content) > options["limit"]:
                result = {"failure": "MODEL_RESPONSE_TOO_LARGE"}
            else:
                result = {"content": base64.b64encode(content).decode("ascii"), "receipt": reply.headers.get("x-request-id"),
                          "transport_window": {"started_at": started_at, "received_at": received_at}}
    except HTTPError as exc:
        exc.close()
        result = {"failure": "MODEL_RATE_LIMITED" if exc.code == 429 else "MODEL_HTTP_ERROR", "status": exc.code}
    except (TimeoutError, OSError, ValueError, KeyError, TypeError):
        result = {"failure": "MODEL_TRANSPORT_UNKNOWN"}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
