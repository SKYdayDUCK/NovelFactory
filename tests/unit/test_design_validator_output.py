"""Independent check runs keep the archived design receipt intact."""
import json
from pathlib import Path
import subprocess
import sys


def test_validator_writes_selected_output_outside_checkout(tmp_path):
    root = Path(__file__).resolve().parents[2]
    archived = root / "validation/package_check_results.json"
    original = archived.read_bytes()
    report = tmp_path / "reports" / "static-check.json"
    run = subprocess.run(
        [sys.executable, root / "validation/validate_package.py", "--output", report],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    result = json.loads(report.read_text())
    assert result == json.loads(run.stdout)
    assert result["status"] == "PASS"
    assert result["counts"]["r1_planned_acceptance_checks"] == 37
    assert archived.read_bytes() == original
