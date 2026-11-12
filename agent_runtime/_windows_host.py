"""Win32 host lifetime. The launcher joins the job before it starts the host."""
import subprocess
import sys
from uuid import uuid4


def create_job():
    import win32job

    name = "Local\\NovelFactory-" + uuid4().hex
    job = win32job.CreateJobObject(None, name)
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    except BaseException:
        job.Close()
        raise
    return job, name


def main():
    import win32api
    import win32job

    # Only the supervising process keeps a handle. Losing it terminates every
    # process in this job, even after the original host has already exited.
    job = win32job.OpenJobObject(win32job.JOB_OBJECT_ASSIGN_PROCESS, False, sys.argv[1])
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    finally:
        job.Close()
    # With close_fds=True Windows does not automatically propagate inherited
    # pipe handles across another CreateProcess. Pass only this stdio triplet.
    return subprocess.call(sys.argv[2:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
