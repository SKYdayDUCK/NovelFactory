import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/install_p5_project.py"


def test_project_setup_preserves_other_settings_and_serializes_competing_bindings(tmp_path):
    directory = tmp_path / ".codex"
    directory.mkdir()
    config = directory / "config.toml"
    config.write_text('model = "other-model"\n[mcp_servers.other]\ncommand="other"\n')
    def install(alias):
        return subprocess.run([sys.executable, SCRIPT, "--project", tmp_path, "--ssh-alias", alias],
                              check=False, capture_output=True, text=True)
    aliases = ["first-service", "second-service"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(install, aliases))
    assert sorted(result.returncode for result in results) == [0, 1]
    winner = aliases[next(i for i, result in enumerate(results) if result.returncode == 0)]
    parsed = tomllib.loads(config.read_text())
    assert parsed["model"] == "other-model"
    assert parsed["mcp_servers"]["other"] == {"command": "other"}
    assert parsed["mcp_servers"]["novelfactory"]["args"][-1] == winner
    before = config.read_bytes()
    assert install(winner).stdout.strip() == "ALREADY_CONFIGURED"
    assert config.read_bytes() == before
