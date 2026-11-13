"""Single-host file publication and locking on POSIX and local Windows NTFS."""
import os
from contextlib import closing, contextmanager
from pathlib import Path


def sync_directory(path):
    # Windows publication below performs a write-through move. Its CRT cannot
    # open directories for fsync. On POSIX the parent names need explicit sync.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_file(source, destination, *, replace=False):
    if os.name == "nt":
        import pywintypes
        import win32file
        import winerror

        # No COPY_ALLOWED: both names must reside on the same local volume.
        flags = win32file.MOVEFILE_WRITE_THROUGH
        if replace:
            flags |= win32file.MOVEFILE_REPLACE_EXISTING
        try:
            win32file.MoveFileEx(str(source), str(destination), flags)
        except pywintypes.error as error:
            if error.winerror in (winerror.ERROR_ALREADY_EXISTS, winerror.ERROR_FILE_EXISTS):
                raise FileExistsError(destination) from error
            raise
    elif replace:
        os.replace(source, destination)
    else:
        os.link(source, destination)
    sync_directory(Path(destination).parent)


@contextmanager
def private_export(destination):
    """Publish complete owner-private bytes at a new local path on POSIX or Windows."""
    from .private_files import private_temporary_file

    destination = Path(destination)
    descriptor, temporary = private_temporary_file(destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as pending:
            yield pending
            pending.flush()
            os.fsync(pending.fileno())
        publish_file(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def file_lock(path, *, exclusive=True):
    if os.name == "nt":
        import pywintypes
        import win32con
        import win32file

        with closing(win32file.CreateFile(str(path), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                                  win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
                                  win32con.OPEN_ALWAYS, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)) as handle:
            if win32file.GetFileInformationByHandle(handle)[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Invalid lock file")
            offset = pywintypes.OVERLAPPED()
            flags = win32con.LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
            win32file.LockFileEx(handle, flags, 1, 0, offset)
            try:
                yield
            finally:
                win32file.UnlockFileEx(handle, 1, 0, offset)
    else:
        import fcntl

        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)


def open_append(path):
    """Open an existing ledger without following a final symlink/reparse point."""
    if os.name == "nt":
        import msvcrt

        import win32con
        import win32file

        handle = win32file.CreateFile(str(path), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ,
                                      None, win32con.OPEN_EXISTING, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        try:
            if win32file.GetFileInformationByHandle(handle)[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Invalid ledger file")
            fd = msvcrt.open_osfhandle(int(handle), os.O_APPEND | os.O_WRONLY | os.O_BINARY)
        except BaseException:
            handle.Close()
            raise
        handle.Detach()
    else:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    return os.fdopen(fd, "ab")


def lock_is_held(path):
    """Probe the existing worker's exclusive lock without waiting or creating a file."""
    if os.name == "nt":
        import pywintypes
        import win32con
        import win32file
        import winerror

        try:
            handle = win32file.CreateFile(str(path), win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
                win32con.OPEN_EXISTING, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        except pywintypes.error as error:
            if error.winerror in (winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_PATH_NOT_FOUND):
                return False
            raise
        with closing(handle):
            if win32file.GetFileInformationByHandle(handle)[0] & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Invalid worker lock")
            offset = pywintypes.OVERLAPPED()
            try:
                win32file.LockFileEx(handle, win32con.LOCKFILE_FAIL_IMMEDIATELY, 1, 0, offset)
            except pywintypes.error as error:
                if error.winerror == winerror.ERROR_LOCK_VIOLATION:
                    return True
                raise
            win32file.UnlockFileEx(handle, 1, 0, offset)
            return False
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)
