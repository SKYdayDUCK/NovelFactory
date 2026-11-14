"""Metadata subprocesses must never inherit the live MCP input stream."""
import subprocess

from novel_factory.application import adaptive_instructions


def test_git_metadata_queries_use_independent_stdio(monkeypatch):
    commands = []

    def git_metadata(command, **options):
        # A server may already be reading stdin. An inherited handle blocks
        # Git for Windows and its surviving launcher child can defeat timeout.
        assert options["stdin"] == subprocess.DEVNULL
        assert options["stderr"] == subprocess.PIPE
        assert options["timeout"] <= 5
        commands.append(command)
        return "1" * 40 + "\n" if options.get("text") else b""

    monkeypatch.setattr(adaptive_instructions.subprocess, "check_output", git_metadata)
    release = adaptive_instructions._code_release()
    assert release["source_commit"] == "1" * 40 and release["origin"] == "CHECKOUT"
    assert len(commands) == 2
