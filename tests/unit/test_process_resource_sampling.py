"""Benchmark metrics retain CPU/peak semantics on the native Windows host."""
import os

import pytest

from benchmarks.process_server import _resource_usage


@pytest.mark.skipif(os.name != "nt", reason="Windows FILETIME and PSAPI units")
def test_windows_sampling_converts_cpu_ticks_and_uses_peak_not_current_rss(monkeypatch):
    import win32process

    monkeypatch.setattr(win32process, "GetProcessTimes", lambda _: {
        "UserTime": 15_000_000, "KernelTime": 2_500_000})
    monkeypatch.setattr(win32process, "GetProcessMemoryInfo", lambda _: {
        "PeakWorkingSetSize": 8 * 1024 * 1024, "WorkingSetSize": 1024 * 1024})
    assert _resource_usage() == {
        "user_cpu_seconds": 1.5, "system_cpu_seconds": 0.25, "peak_rss_kib": 8192}


def test_native_sampling_reports_real_cpu_and_lifetime_peak():
    before = _resource_usage()
    # Touch actual memory pages, rather than relying on allocator reservation.
    data = bytearray(8 * 1024 * 1024)
    for index in range(0, len(data), 4096):
        data[index] = 1
    sum(value * value for value in range(100_000))
    after = _resource_usage()
    assert after["user_cpu_seconds"] >= before["user_cpu_seconds"] >= 0
    assert after["system_cpu_seconds"] >= before["system_cpu_seconds"] >= 0
    assert after["peak_rss_kib"] >= before["peak_rss_kib"] > 0
