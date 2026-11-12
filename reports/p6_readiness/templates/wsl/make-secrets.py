"""Create fresh local database and Redis secrets inside the operator's private directory."""

import hashlib
import os
from pathlib import Path
import secrets
import stat


def main():
    root = Path(os.environ["P6_SECRETS"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
    ):
        raise SystemExit("Use an owned 0700 Linux directory")
    names = ["nf_admin", "aw_admin", "nf_password", "aw_password", "redis_password", "redis.acl"]
    if any((root / n).exists() for n in names):
        raise SystemExit("Existing secrets must be reused with their database volumes")
    passwords = {n: secrets.token_hex(32) for n in names[:-1]}
    hashed = hashlib.sha256(passwords["redis_password"].encode()).hexdigest()
    passwords["redis.acl"] = f"user default on #{hashed} ~* &* +@all"
    for name, value in passwords.items():
        # Docker Compose file-backed secrets are bind mounts: the image service
        # UID needs read permission. The host's 0700 parent protects the files.
        mode = 0o600 if name == "redis_password" else 0o444
        with os.fdopen(
            os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as out:
            out.write(value + "\n")
            os.fchmod(out.fileno(), mode)


if __name__ == "__main__":
    main()
