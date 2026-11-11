"""Run the original 200-Agent CI overload with 80% shared subjects and five burst rounds.

This uses precisely the same cloned database, object and metric wiring as
scion_paired.py; the additional upstream workload phase is selected by the
existing benchmark's "overload" argument.
"""
import os
import sys
from unittest.mock import patch

import scion_paired as paired


def main():
    if sys.argv[1:] == ["--child"]:
        from benchmarks import p2_behavioral_load
        run = p2_behavioral_load.run_benchmark

        def overload(*args, **kwargs):
            assert kwargs["profile"] == "hotspot" and kwargs["hot_fraction"] == .8
            return run(*args, **{**kwargs, "profile": "overload"})

        with patch.object(p2_behavioral_load, "run_benchmark", overload):
            paired.child()
    else:
        # The coordinator launches a new source-isolated interpreter per arm.
        with patch.object(paired, "__file__", __file__):
            paired.main()


if __name__ == "__mp_main__" and os.environ.get("NF_SCION_METER_DIR"):
    paired.install_worker_meter()

if __name__ == "__main__":
    main()
