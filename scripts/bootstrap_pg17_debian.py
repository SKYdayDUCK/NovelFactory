"""Download checksum-pinned PostgreSQL 17.11 binaries for Debian 13 amd64.

No root permission is needed. Other operating systems should install PostgreSQL
17 through their package manager or use docker-compose.yml.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import urllib.request


PACKAGES = {
    "libpq5_17.11-0+deb13u1_amd64.deb": "20a4c9ef58b4baf90deda67cfb2cc062871c062830dddc234d28f5ac7931b86b",
    "postgresql-17_17.11-0+deb13u1_amd64.deb": "d2ce1ddffafa783f9acda4c92c86fc21e8288bff3782d739294f14fa797d7886",
    "postgresql-client-17_17.11-0+deb13u1_amd64.deb": "9d8558f8dd57c8e92e218a20698383575d53742ca3f9e7c2b7fe5f246d5216ae",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("these binaries require Debian 13 amd64 (or a compatible ABI)")
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    dist = destination / "dist"
    dist.mkdir(exist_ok=True)
    records = []
    for name, digest in PACKAGES.items():
        url = f"https://deb.debian.org/debian/pool/main/p/postgresql-17/{name}"
        target = destination / name
        if not target.exists():
            print("Downloading", name, flush=True)
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise RuntimeError(f"download checksum mismatch: {name}")
            target.write_bytes(payload)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"checksum mismatch: {target}")
        subprocess.run(["dpkg-deb", "-x", str(target), str(dist)], check=True)
        records.append({"url": url, "sha256": digest, "bytes": target.stat().st_size})
    bin_path = dist / "usr/lib/postgresql/17/bin"
    subprocess.run([str(bin_path / "postgres"), "--version"], check=True)
    (destination / "downloads.json").write_text(json.dumps(records, indent=2) + "\n")
    print("PG_BIN=" + str(bin_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
