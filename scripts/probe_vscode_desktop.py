"""Drive two author prompts through the native VS Code desktop and the official Codex VSIX.

Run under Xvfb in a loopback-only namespace with a fresh migrated PG17 database.
The companion GitHub workflow provisions the pinned binaries and Python dependencies.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time

from probe_vscode_offline_transport import SyntheticResponses, ThreadingHTTPServer

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import profiles
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


ROOT = Path(__file__).resolve().parents[1]
VSIX_SHA = "4af1f59f7d8a0145943c2ec583e0bf09a495c258304d113052e74cbaeb73d6f6"
VERSION = "1.145.3"
EXTENSION = "openai.chatgpt@26.51106.51428"
PROMPT = "继续未完成的任务"


def prepare_sample(base, model_url):
    """Seed an already accepted predecessor with its real acceptance event, without inference."""
    private = base / "private"
    store = ObjectStore(base / "objects")
    bootstrap = subprocess.check_output(
        [sys.executable, ROOT / "scripts/bootstrap_p5_m0.py", "--private-dir", private,
         "--route", "external", "--hours", "1"], text=True,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "NF_MODEL_ENDPOINT": model_url.replace("http:", "https:"),
             "NF_MODEL_NAME": "nf-ui-test", "NF_MODEL_API_KEY": "synthetic-setup-only",
             "NF_OBJECT_ROOT": str(store.root)},
    )
    sample = json.loads(bootstrap)
    db = Database()
    try:
        kernel = Kernel(db, store)
        tid = sample["producer"]
        with db.uow(write=False) as repo:
            initial = repo.one("tasks", {"task_id": tid})
            spec = repo.one("task_inputs", {"task_id": tid, "generation": 0})["spec"]
            job = repo.one("supervisor_jobs", {"task_id": tid})["config"]
        owner = Principal(initial["grant_id"], initial["principal_id"])
        task = kernel.claim_task(owner, "desktop-fixture", task_id=tid, lease_seconds=600)
        cut = WorldQueryService(kernel).context_compile(owner, {
            "schema_version": "2.1", "task_id": tid, "generation": task["generation"],
            "recipe_id": profiles()[task["role_id"]]["allowed_recipes"][0],
            "input_manifest_ref": job["manifest_ref"], "knowledge_cut": job["knowledge_cut"],
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": spec["world_ref"]},
                              "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE",
                              "max_wait_ms": 0},
            "subject_time": {"kind": "POINT", "at": spec["story_time"]},
            "token_budget": job["context_tokens"], "idempotency_key": "desktop-fixture-context",
        })
        assert cut["status"] == "READY"
        with db.uow() as repo:
            kernel.tasks.accept_in_transaction(repo.conn, tid, task["generation"])
        assert kernel.tasks.get(sample["waiting"])["state"] == "BLOCKED"
    finally:
        db.close()
    return private / "resume.json", sample


def configure(base, binding, model_url):
    for name in ("home", "codex", "run", "project", "records", "profile/User", "extensions"):
        (base / name).mkdir(parents=True, mode=0o700)
    command = json.dumps(sys.executable)
    args = json.dumps([str(ROOT / "scripts/probe_vscode_offline_transport.py"), "capture",
                       "--binding", str(binding), "--record-dir", str(base / "records")])
    (base / "codex/config.toml").write_text(f'''model = "nf-ui-test"
model_provider = "nf_ui"
[model_providers.nf_ui]
name = "Loopback deterministic provider"
base_url = "{model_url}"
wire_api = "responses"
supports_websockets = false
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
[projects.{json.dumps(str(base / 'project'))}]
trust_level = "trusted"
[mcp_servers.novelfactory]
command = {command}
args = {args}
startup_timeout_sec = 30
[mcp_servers.novelfactory.tools.nf_resume_project]
approval_mode = "approve"
''')
    (base / "profile/User/settings.json").write_text(json.dumps({
        "telemetry.telemetryLevel": "off", "update.mode": "none", "extensions.autoUpdate": False,
        "extensions.autoCheckUpdates": False, "security.workspace.trust.enabled": False,
        "workbench.startupEditor": "none", "workbench.secondarySideBar.defaultVisibility": "visible",
        "chatgpt.openOnStartup": True, "chatgpt.localeOverride": "en-US",
        "workbench.welcomePage.walkthroughs.openOnInstall": False,
    }))
    (base / "profile/User/keybindings.json").write_text(json.dumps([
        {"key": "ctrl+alt+shift+9", "command": "chatgpt.openSidebar"}
    ]))
    return {
        "HOME": str(base / "home"), "CODEX_HOME": str(base / "codex"), "PATH": os.defpath,
        "XDG_RUNTIME_DIR": str(base / "run"), "DISPLAY": os.environ["DISPLAY"],
        "XAUTHORITY": os.environ["XAUTHORITY"], "LANG": "C.UTF-8", "NO_PROXY": "*",
    }


def observations(base):
    records = []
    for path in sorted((base / "records").glob("*.jsonl")):
        lines = path.read_bytes().split(b"\n")[:-1]
        records.append([json.loads(line) for line in lines])
    return records


def webview(frame):
    while frame is not None:
        if frame.url.startswith("vscode-webview:") or "extensionId=openai.chatgpt" in frame.url:
            return True
        frame = frame.parent_frame
    return False


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def startup_diagnostics(base, desktop, helper):
    # Retain fixed categories only. Chromium and the extension may log private paths or payloads.
    raw = (base / "desktop.log").read_text(errors="replace").lower()
    patterns = {
        "sandbox_helper_permissions": r"suid sandbox helper binary|owned by root and has mode 4755",
        "namespace_permission": r"failed to move to new namespace|namespace sandbox.*not permitted",
        "no_usable_sandbox": r"no usable sandbox|no supported sandbox",
        "zygote_failure": r"fatal.*zygote|zygote.*failed",
        "missing_x_server": r"missing x server|cannot open display|unable to open x display",
        "x_authorization": r"authorization required|invalid mit-magic-cookie",
        "shared_library_missing": r"error while loading shared libraries",
        "shared_memory_failure": r"unable to access.*dev/shm|no space left on device",
        "profile_lock": r"processsingleton|failed to create.*singleton",
        "crashpad_failure": r"crashpad.*(failed|required|denied)",
        "debug_listener_started": r"devtools listening",
        "address_in_use": r"address already in use",
        "fatal": r"fatal[:\]]",
    }
    meta = helper.stat()
    return {"exit_code": desktop.poll(), "adjacent_sandbox_mode": oct(meta.st_mode & 0o7777),
            "adjacent_sandbox_uid": meta.st_uid,
            "stderr_markers": [key for key, pattern in patterns.items() if re.search(pattern, raw)]}


def exercise(base, desktop, cdp_port, evidence):
    from playwright.sync_api import Error, sync_playwright

    start = time.monotonic()
    with sync_playwright() as pw:
        while True:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}", timeout=2000)
                break
            except Error:
                if desktop.poll() is not None or time.monotonic() - start > 40:
                    raise RuntimeError("Desktop failed to create the CDP listener") from None
                time.sleep(.2)
        page = None
        for _ in range(90):
            for context in browser.contexts:
                page = next((p for p in context.pages if "workbench.html" in p.url), None)
                if page:
                    break
            if page:
                break
            time.sleep(.3)
        if not page:
            raise RuntimeError("Native workbench renderer not found")
        page.locator(".monaco-workbench").wait_for(timeout=45000)
        evidence["stage"] = "open_codex_sidebar"
        page.keyboard.press("Control+Alt+Shift+9")
        editor = None
        for _ in range(100):
            for frame in page.frames:
                if not webview(frame):
                    continue
                try:
                    for possible in frame.locator('[contenteditable="true"]').all():
                        if possible.is_visible() and possible.is_editable():
                            editor = possible
                            break
                    if editor:
                        break
                    body = frame.locator("body").inner_text(timeout=400).lower()
                    evidence["webview_observed"] = True
                    evidence["sign_in_screen"] = "sign in" in body
                except Error:
                    continue
            if editor:
                break
            time.sleep(.5)
        if editor is None:
            raise RuntimeError("No editable composer in the Codex webview")
        for i in range(2):
            evidence["stage"] = f"ui_turn_{i + 1}"
            editor.click(timeout=10000)
            page.keyboard.insert_text(PROMPT)
            if PROMPT not in editor.inner_text(timeout=2000):
                raise RuntimeError("Prompt did not reach the extension composer")
            editor.press("Enter")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                rows = [r for connection in observations(base) for r in connection]
                if sum("receipt" in r for r in rows) >= i + 1:
                    # Wait for the extension to render the model's completion before the next turn.
                    if frame.get_by_text("已收到。", exact=True).count() >= i + 1:
                        awakened = sorted(r["receipt"]["awakened"] for r in rows if "receipt" in r)
                        if awakened != ([1] if i == 0 else [0, 1]):
                            raise RuntimeError("Unexpected wake count after the submitted turn")
                        evidence.setdefault("awakened_by_ui_turn", []).append(1 - i)
                        break
                time.sleep(.25)
            else:
                raise RuntimeError("No completed renderer turn and matching MCP receipt")
        evidence["rendered_author_turns"] = 2


def verify(base, sample, evidence):
    records = observations(base)
    evidence["stdio_by_connection"] = records
    calls = []
    for connection in records:
        if not any("receipt" in row for row in connection):
            continue
        inventories = [set(r["tools"]) for r in connection if "tools" in r]
        if not inventories or any(t != {"nf_project_status", "nf_resume_project"} for t in inventories):
            raise RuntimeError("The calling connection lacks independent tools/list evidence")
        for row in connection:
            if row.get("method") == "tools/call":
                if row.get("tool") != "nf_resume_project" or not row.get("empty_arguments"):
                    raise RuntimeError("The UI called an unexpected tool or supplied scope arguments")
                calls.append(row)
    receipts = [r for group in records for r in group if "receipt" in r]
    if len(calls) != 2 or len(receipts) != 2 or any(r.get("isError") is not False for r in receipts):
        raise RuntimeError("Expected precisely two successful bound MCP calls")
    # The extension can replace its MCP process across turns. Verify each transition at submission too.
    if sorted(r["receipt"]["awakened"] for r in receipts) != [0, 1]:
        raise RuntimeError("The two calls did not produce exactly one wake")
    for row in receipts:
        receipt = row["receipt"]
        if receipt["counts"] != {"READY": 1, "WAITING": 0, "RUNNING": 0, "ACCEPTED": 1}:
            raise RuntimeError("Bound project state differs from the two-task fixture")
    db = Database()
    try:
        with db.uow(write=False) as repo:
            assert repo.one("tasks", {"task_id": sample["waiting"]})["state"] == "READY"
            assert len(repo.many("inbox", {"consumer": f"workflow:{sample['waiting']}:0"})) == 1
            assert not repo.many("call_attempts")
            evidence["postgres"] = repo.conn.exec_driver_sql("SHOW server_version").scalar_one()
            evidence["database_after"] = {"accepted": 1, "ready": 1, "acknowledged_events": 1,
                                          "novelfactory_provider_attempts": 0}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", type=Path, required=True, help="Extracted VSCode-linux-x64")
    parser.add_argument("--vsix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Public, filtered JSON evidence")
    args = parser.parse_args()
    evidence = {"status": "FAILED", "stage": "preflight"}
    try:
        interfaces = {row.split(":", 1)[0].strip()
                      for row in Path("/proc/net/dev").read_text().splitlines()[2:]}
        if os.getuid() == 0 or interfaces != {"lo"} or not os.environ.get("DISPLAY"):
            raise RuntimeError("Requires an ordinary user, loopback-only namespace, and Xvfb DISPLAY")
        args.desktop = args.desktop.resolve()
        args.vsix = args.vsix.resolve()
        with args.vsix.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != VSIX_SHA:
                raise RuntimeError("VSIX SHA-256 mismatch")
        info = json.loads((args.desktop / "resources/app/product.json").read_text())
        version = json.loads((args.desktop / "resources/app/package.json").read_text())["version"]
        if version != VERSION or info["commit"] != "dcd3ad6e368fbe19d03f37054a40dbaf40d676f4":
            raise RuntimeError("Unexpected native VS Code desktop build")
        evidence.update(desktop=version, commit=info["commit"], extension=EXTENSION,
                        vsix_sha256=VSIX_SHA, ordinary_uid=os.getuid(), network=["lo"],
                        model="synthetic loopback Responses", display="Xvfb")
        with tempfile.TemporaryDirectory(prefix="nf-desktop-") as temp, ExitStack() as stack:
            base = Path(temp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticResponses)
            stack.callback(server.server_close)
            Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
            model_url = f"http://127.0.0.1:{server.server_port}/v1"
            evidence["stage"] = "prepare_sample"
            binding, sample = prepare_sample(base, model_url)
            env = configure(base, binding, model_url)
            dirs = ["--user-data-dir", base / "profile", "--extensions-dir", base / "extensions"]
            cli = [args.desktop / "bin/code", *dirs]
            evidence["stage"] = "install_official_vsix"
            subprocess.run([*cli, "--install-extension", args.vsix, "--force"],
                           env=env, cwd=base, check=True, timeout=90, stdout=subprocess.DEVNULL)
            inventory = subprocess.check_output([*cli, "--list-extensions", "--show-versions"],
                                                text=True, env=env, cwd=base, timeout=30)
            if EXTENSION not in inventory.splitlines():
                raise RuntimeError("VS Code did not list the pinned installed extension")
            (binary,) = (base / "extensions").glob("openai.chatgpt-*/bin/linux-x86_64/codex")
            evidence["bundled_codex"] = subprocess.check_output(
                [binary, "--version"], env=env, text=True, timeout=10).strip()
            evidence["stage"] = "launch_workbench"
            with socket.socket() as free_port:
                free_port.bind(("127.0.0.1", 0))
                cdp_port = free_port.getsockname()[1]
            desktop_log = stack.enter_context((base / "desktop.log").open("wb"))
            desktop = subprocess.Popen(
                [args.desktop / "code", *dirs, "--disable-gpu", "--password-store=basic",
                 "--disable-updates", "--disable-crash-reporter", "--enable-logging=stderr",
                 f"--remote-debugging-port={cdp_port}",
                 "--remote-debugging-address=127.0.0.1", "--skip-welcome", base / "project"],
                env=env, cwd=base, stdout=desktop_log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            stack.callback(terminate, desktop)
            try:
                exercise(base, desktop, cdp_port, evidence)
                evidence["stage"] = "verify_postgres_and_mcp"
                verify(base, sample, evidence)
            finally:
                evidence["desktop_startup"] = startup_diagnostics(
                    base, desktop, args.desktop / "chrome-sandbox")
                evidence["stdio_by_connection"] = observations(base)
        evidence.update(status="PASS", stage="complete")
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            evidence["reason"] = str(exc)
        raise SystemExit(1) from None
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: evidence[k] for k in ("status", "stage", "desktop_startup")
                          if k in evidence}), flush=True)


if __name__ == "__main__":
    main()
