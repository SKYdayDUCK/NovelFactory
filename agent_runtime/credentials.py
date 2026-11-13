"""Operator credentials, separate from model catalogs, task inputs and Git.

Environment variables take precedence. Windows uses Credential Manager; POSIX
uses owner-private files in the user's application data directory.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

PREFIX = "NovelFactory/provider/"


class CredentialError(ValueError):
    pass


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name):
        raise CredentialError("INVALID_CREDENTIAL_REFERENCE")
    return name


def _directory(*, create=False):
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    directory = base / "novel-factory/credentials"
    if create:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        meta = directory.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077):
            raise CredentialError("CREDENTIAL_STORE_NOT_PRIVATE")
    return directory


def _stored(name):
    if os.name == "nt":
        import pywintypes
        import win32cred

        try:
            blob = win32cred.CredRead(PREFIX + name, win32cred.CRED_TYPE_GENERIC)["CredentialBlob"]
            return blob.decode("utf-16-le") if isinstance(blob, bytes) else blob
        except pywintypes.error as error:
            if error.winerror == 1168:  # ERROR_NOT_FOUND
                return None
            raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
    path = _directory() / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & 0o077 or meta.st_size > 32768):
            raise CredentialError("CREDENTIAL_STORE_NOT_PRIVATE")
        return stream.read(32769).decode("utf-8")


def resolve(name):
    """Return a credential only to the authorized runtime, never to a catalog."""
    name = _name(name)
    return os.environ.get(name) or _stored(name)


def put(name, value):
    name = _name(name)
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 32768
            or any(c in value for c in "\r\n\0")):
        raise CredentialError("INVALID_CREDENTIAL_VALUE")
    if os.name == "nt":
        import win32cred

        if len(value.encode("utf-16-le")) > 2560:
            raise CredentialError("CREDENTIAL_TOO_LARGE_FOR_WINDOWS_VAULT")
        win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": PREFIX + name,
                            "UserName": name, "CredentialBlob": value,
                            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
        return
    directory = _directory(create=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove(name):
    name = _name(name)
    if os.name == "nt":
        import pywintypes
        import win32cred

        try:
            win32cred.CredDelete(PREFIX + name, win32cred.CRED_TYPE_GENERIC, 0)
        except pywintypes.error as error:
            if error.winerror != 1168:
                raise CredentialError("CREDENTIAL_STORE_UNAVAILABLE") from None
    else:
        (_directory() / name).unlink(missing_ok=True)


def status(name):
    name = _name(name)
    source = "environment" if os.environ.get(name) else "local-store" if _stored(name) else None
    return {"reference": name, "configured": source is not None, "source": source}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("set", "status", "remove"))
    parser.add_argument("reference")
    parser.add_argument("--stdin", action="store_true", help="Read a secret from stdin instead of a masked prompt")
    args = parser.parse_args()
    try:
        if args.action == "set":
            value = sys.stdin.read(32770).rstrip("\r\n") if args.stdin else getpass.getpass("API key: ")
            put(args.reference, value)
        elif args.action == "remove":
            remove(args.reference)
        print(json.dumps(status(args.reference)))
    except (CredentialError, OSError, UnicodeError):
        parser.exit(2, "Credential operation failed; check the reference and private store access.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
