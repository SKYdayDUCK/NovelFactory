"""Switch actual historical and frozen current source in separate processes."""
from pathlib import Path
import argparse
import os
import subprocess

root = Path(__file__).resolve().parent
workspace = root.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", type=Path, default=workspace / "NovelFactory-p1-baseline")
parser.add_argument("--p2", type=Path, default=root / "frozen-p2")
parser.add_argument("--p3", type=Path, default=root / "frozen-p3")
parser.add_argument("--baseline-python", type=Path)
parser.add_argument("--target-python", type=Path, default=workspace / "NovelFactory/.venv/bin/python")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
baseline = args.baseline.resolve()
legacy_python = args.baseline_python or baseline / ".venv/bin/python"
current_python = args.target_python.resolve()
output = args.output.resolve()
if output.exists():
    parser.error("Use a new output directory; historical evidence is never overwritten")

for label, source, python in [("p1", baseline, legacy_python),
                               ("p2", args.p2.resolve(), current_python),
                               ("p3", args.p3.resolve(), current_python)]:
    env = {**os.environ, "PYTHONPATH": str(source / "src")}
    subprocess.run([str(python), "-m", "alembic", "upgrade", "head"],
                   cwd=source, env=env, check=True)
    subprocess.run([str(python), str(root / "check_stage.py"), "seed" if label == "p1" else "verify",
                    "--source-root", str(source), "--output", str(output), "--label", label],
                   cwd=source, env=env, check=True)
