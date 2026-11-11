"""Process-local experiment switches and counters, with no authority over world data."""
import os
from collections import Counter
from threading import Lock


class ScionOptions:
    def __init__(self):
        # The original switch remains an explicit group override. A per-path
        # setting takes precedence so deployments can roll back independently.
        group = os.getenv("NF_SCION_COLUMNS")
        self.worker_columns = os.getenv("NF_SCION_WORKER_COLUMNS", "1" if group is None else group) == "1"
        self.review_columns = os.getenv("NF_SCION_REVIEW_COLUMNS", group or "0") == "1"
        self.query_columns = os.getenv("NF_SCION_QUERY_COLUMNS", group or "0") == "1"
        self.scopes = os.environ.get("NF_SCION_SCOPES") == "1"
        self.replay = os.environ.get("NF_SCION_REPLAY") == "1"
        self._counts = Counter()
        self._lock = Lock()

    @property
    def columns(self):
        """Compatibility view used by the first-round all-on/all-off experiments."""
        return self.worker_columns and self.review_columns and self.query_columns

    @columns.setter
    def columns(self, enabled):
        self.worker_columns = self.review_columns = self.query_columns = enabled

    def record(self, path, outcome):
        with self._lock:
            self._counts[f"{path}.{outcome}"] += 1

    def counts(self):
        with self._lock:
            return dict(self._counts)
