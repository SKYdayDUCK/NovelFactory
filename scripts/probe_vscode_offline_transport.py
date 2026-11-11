"""Local validation fixtures for the published VS Code extension. No external model."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit


class LocalEndpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, content_type, data):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WebviewAssets(LocalEndpoint):
    def do_GET(self):
        path = urlsplit(self.path).path
        filename = path.rsplit("/", 1)[-1]
        if "/out/vs/workbench/contrib/webview/browser/pre/" not in path or filename not in {
            "index.html", "fake.html", "service-worker.js"
        }:
            self.send_error(404)
            return
        data = (self.server.static_dir / filename).read_bytes()
        self.reply("text/javascript" if filename.endswith(".js") else "text/html", data)


class SyntheticResponses(LocalEndpoint):
    """Select only the advertised project resume function on the author's turn."""
    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        if self.path != "/v1/responses" or not 0 < size <= 8_000_000:
            self.send_error(400)
            return
        request = json.loads(self.rfile.read(size))
        inputs = [item for item in request.get("input", []) if isinstance(item, dict)]
        last_user = max((i for i, item in enumerate(inputs) if item.get("role") == "user"), default=-1)
        answered = any(item.get("type") == "function_call_output" for item in inputs[last_user + 1:])
        tool = any(group.get("type") == "namespace" and group.get("name") == "mcp__novelfactory"
                   and any(tool.get("name") == "nf_resume_project" for tool in group.get("tools", group.get("functions", [])))
                   for group in request.get("tools", []))
        if tool and not answered:
            item = {"id": "fc_nf", "type": "function_call", "call_id": f"call_nf_{last_user}",
                    "namespace": "mcp__novelfactory", "name": "nf_resume_project", "arguments": "{}", "status": "completed"}
        else:
            item = {"id": "msg_nf", "type": "message", "role":"assistant", "status":"completed",
                    "content":[{"type":"output_text", "text":"已收到。", "annotations":[]}]}
        response = {"id":"nf_reply","object":"response","created_at":1,"model":"nf-ui-test","status":"in_progress","output":[]}
        events = [
            ("response.created", {"response": response}),
            ("response.output_item.added", {"output_index":0, "item":{**item,"status":"in_progress"}}),
            ("response.output_item.done", {"output_index":0, "item":item}),
            ("response.completed", {"response":{**response,"status":"completed","output":[item]}}),
        ]
        stream = "".join(f"event: {name}\ndata: " + json.dumps({"type":name,"sequence_number":i,**fields}) + "\n\n"
                         for i,(name,fields) in enumerate(events))
        self.reply("text/event-stream", stream.encode())


def serve(args):
    # This harness is specifically for offline acceptance with a fresh synthetic profile.
    interfaces = {line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]}
    if interfaces != {"lo"}:
        raise SystemExit("Run this validation inside a network namespace containing only loopback")
    assets = ThreadingHTTPServer(("127.0.0.1", args.https_port), WebviewAssets)
    assets.static_dir = args.static_dir
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    assets.socket = tls.wrap_socket(assets.socket, server_side=True)
    Thread(target=assets.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.model_port), SyntheticResponses).serve_forever()


def capture(args):
    record = args.record.open("x", encoding="utf-8", buffering=1)
    process = subprocess.Popen([sys.executable, "-m", "novel_factory.interfaces.mcp_stdio", "--binding", str(args.binding)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pending = {}

    def relay(source, destination, request_side):
        for line in source:
            message = json.loads(line)
            observation = None
            if request_side and "method" in message:
                method = message["method"]
                if "id" in message:
                    pending[message["id"]] = method
                observation = {"direction":"extension_to_server","method":method}
                if method == "tools/call":
                    params = message.get("params", {})
                    observation.update(tool=params.get("name"), empty_arguments=params.get("arguments") == {})
            elif not request_side:
                method = pending.pop(message.get("id"), None)
                result = message.get("result", {})
                if method == "tools/list":
                    observation = {"direction":"server_to_extension", "tools":[t["name"] for t in result.get("tools", [])]}
                elif method == "tools/call":
                    receipt = result.get("structuredContent", {})
                    observation = {"direction":"server_to_extension","isError":result.get("isError"),
                                   "receipt":{k:receipt.get(k) for k in ("counts","ready_count","awakened","recovered")}}
            if observation is not None:
                record.write(json.dumps(observation) + "\n")
            destination.write(line)
            destination.flush()
        destination.close()

    Thread(target=relay,args=(sys.stdin.buffer,process.stdin,True),daemon=True).start()
    try:
        relay(process.stdout, sys.stdout.buffer, False)
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        record.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    offline = modes.add_parser("serve")
    for option in ("static-dir", "cert", "key"):
        offline.add_argument("--" + option, type=Path, required=True)
    offline.add_argument("--https-port", type=int, default=31185)
    offline.add_argument("--model-port", type=int, default=31186)
    audit = modes.add_parser("capture")
    audit.add_argument("--binding", type=Path, required=True)
    audit.add_argument("--record-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "serve":
        return serve(args)
    from uuid import uuid4
    args.record = args.record_dir / f"stdio-{uuid4()}.jsonl"
    return capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
