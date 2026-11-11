"""Process-local experiment switches and counters, with no authority over world data."""
import os
from collections import Counter
from threading import Lock


class ScionOptions:
    def __init__(self):
        self.columns = os.environ.get("NF_SCION_COLUMNS") == "1"
        self.scopes = os.environ.get("NF_SCION_SCOPES") == "1"
        self.replay = os.environ.get("NF_SCION_REPLAY") == "1"
        self._counts = Counter()
        self._lock = Lock()

    def record(self, path, outcome):
        with self._lock:
            self._counts[f"{path}.{outcome}"] += 1

    def counts(self):
        with self._lock:
            return dict(self._counts)
