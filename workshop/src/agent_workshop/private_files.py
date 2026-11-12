"""Read owner-private configuration through the handle whose access was checked."""
from contextlib import closing
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4


def _user_sid():
    import win32api
    import win32con
    import win32security as sec

    with closing(sec.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)) as token:
        return sec.GetTokenInformation(token, sec.TokenUser)[0]


def _check_owner_acl(handle):
    import win32security as sec

    sd = sec.GetSecurityInfo(handle, sec.SE_FILE_OBJECT, sec.OWNER_SECURITY_INFORMATION | sec.DACL_SECURITY_INFORMATION)
    owner = _user_sid()
    acl = sd.GetSecurityDescriptorDacl()
    if sd.GetSecurityDescriptorOwner() != owner or acl is None:
        raise ValueError("Invalid private configuration")
    for index in range(acl.GetAceCount()):
        (kind, _flags), _, *trustee = acl.GetAce(index)
        if kind == sec.ACCESS_DENIED_ACE_TYPE:
            continue
        if kind != sec.ACCESS_ALLOWED_ACE_TYPE or trustee != [owner]:
            raise ValueError("Invalid private configuration")


def _private_security_attributes(*, directory):
    import pywintypes
    import win32security as sec

    owner = sec.ConvertSidToStringSid(_user_sid())
    inheritance = "OICI" if directory else ""
    attrs = pywintypes.SECURITY_ATTRIBUTES()
    attrs.SECURITY_DESCRIPTOR = sec.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{owner}D:P(A;{inheritance};FA;;;{owner})", sec.SDDL_REVISION_1)
    return attrs


def create_private_file(name):
    """Exclusively create a new owner-private file and return its open descriptor."""
    name = Path(name)
    if os.name != "nt":
        return os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    import msvcrt
    import win32con as con
    import win32file

    handle = win32file.CreateFile(str(name), con.GENERIC_READ | con.GENERIC_WRITE, 0,
                                  _private_security_attributes(directory=False), con.CREATE_NEW,
                                  con.FILE_ATTRIBUTE_NORMAL, None)
    try:
        _check_owner_acl(handle)
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
    except BaseException:
        handle.Close()
        name.unlink(missing_ok=True)
        raise
    handle.Detach()
    return fd


def private_temporary_file(directory):
    if os.name != "nt":
        return tempfile.mkstemp(dir=directory)
    name = Path(directory) / (".pending-" + uuid4().hex)
    return create_private_file(name), name


def _checked_handle(path, *, directory=False):
    import pywintypes
    import win32con as con
    import win32file

    flags = win32file.FILE_FLAG_OPEN_REPARSE_POINT | (con.FILE_FLAG_BACKUP_SEMANTICS if directory else 0)
    try:
        handle = win32file.CreateFile(str(path), con.GENERIC_READ | con.READ_CONTROL,
                                      con.FILE_SHARE_READ, None, con.OPEN_EXISTING, flags, None)
    except pywintypes.error as error:
        raise ValueError("Invalid private configuration") from error
    try:
        attr = win32file.GetFileInformationByHandle(handle)[0]
        if (attr & con.FILE_ATTRIBUTE_REPARSE_POINT or bool(attr & con.FILE_ATTRIBUTE_DIRECTORY) != directory
                or win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK):
            raise ValueError("Invalid private configuration")
        _check_owner_acl(handle)
        return handle
    except BaseException:
        handle.Close()
        raise


def read_private_bytes(path, limit):
    if os.name == "nt":
        import msvcrt

        handle = _checked_handle(path)
        try:
            fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except BaseException:
            handle.Close()
            raise
        handle.Detach()  # fd now owns this same verified native handle.
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_size > limit
                or (os.name != "nt" and (meta.st_uid != os.getuid() or meta.st_mode & 0o077))):
            raise ValueError("Invalid private configuration")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Invalid private configuration")
        return data


def check_private_directory(path):
    if os.name == "nt":
        _checked_handle(path, directory=True).Close()
    else:
        meta = Path(path).lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise ValueError("Use an owner-private directory")


def prepare_private_directory(path):
    path = Path(path)
    if os.name != "nt":
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        import pywintypes
        import win32file
        import winerror

        path.parent.mkdir(parents=True, exist_ok=True)
        # Apply the inheritable DACL at creation, before any child can be created.
        attrs = _private_security_attributes(directory=True)
        try:
            win32file.CreateDirectory(str(path), attrs)
        except pywintypes.error as error:
            if error.winerror != winerror.ERROR_ALREADY_EXISTS:
                raise
    check_private_directory(path)
