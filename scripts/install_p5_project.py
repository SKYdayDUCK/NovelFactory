"""Install the project-scoped Codex MCP configuration, using a preconfigured SSH alias."""

import argparse
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "adapters/codex/p5-mcp.toml"


def install(project, alias):
    if not project.is_dir() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", alias):
        raise ValueError("Supply an existing project and an SSH host alias")
    directory = project / ".codex"
    directory.mkdir(exist_ok=True)
    path = directory / "config.toml"
    if path.is_symlink() or directory.is_symlink():
        raise ValueError("Choose the actual directory containing your project configuration")
    # An OS lock survives concurrent installers and releases on process exit.
    # Keep the lock inode stable across atomic configuration replacements.
    with (directory / ".nf-install.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        return _merge(path, alias)


def _merge(path, alias):
    original = path.read_bytes() if path.exists() else None
    content = (original or b"").decode("utf-8")
    config = tomllib.loads(content)
    template = TEMPLATE.read_text(encoding="utf-8").replace('"nf-resume-service"', json.dumps(alias))
    desired = tomllib.loads(template)["mcp_servers"]["novelfactory"]
    current = config.get("mcp_servers", {}).get("novelfactory")
    if current == desired:
        return "ALREADY_CONFIGURED"
    if current is not None:
        raise ValueError("An existing novelfactory MCP entry differs; review that entry before replacement")
    combined = content + ("\n\n" if content else "") + template
    if tomllib.loads(combined)["mcp_servers"]["novelfactory"] != desired:
        raise ValueError("Could not combine the project configuration")
    # Publish a complete file; a competing first install cannot clobber an existing one.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        tmp = Path(file.name)
    try:
        with tmp.open("wb") as out:
            out.write(combined.encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        if original is None:
            try:
                os.link(tmp, path)
            except FileExistsError:
                if path.read_bytes() != tmp.read_bytes():
                    raise ValueError("Project configuration changed concurrently") from None
        else:
            if path.is_symlink() or path.read_bytes() != original:
                raise ValueError("Project configuration changed concurrently")
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return "CONFIGURED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--ssh-alias", default="nf-resume-service")
    args = parser.parse_args()
    try:
        print(install(args.project, args.ssh_alias))
    except (OSError, ValueError, AttributeError) as exc:
        parser.exit(1, f"Configuration unavailable: {exc}\n")


if __name__ == "__main__":
    main()
