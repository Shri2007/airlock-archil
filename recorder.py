"""Append-only event log per run (the audit trail): JSONL + memory. Clones run
in parallel threads, so writes are locked."""
import json
import threading
import time
from pathlib import Path

LOG = Path(__file__).parent / "audit.jsonl"


class Recorder:
    def __init__(self, path=LOG):
        self.path = Path(path)
        self.mem = []
        self.lock = threading.Lock()

    def clear(self):
        self.mem = []
        if self.path.exists():
            self.path.unlink()

    def record(self, run, kind, target, leaked=None):
        e = {"run": run, "ts": time.time(), "kind": kind,
             "target": target, "leaked": leaked or []}
        with self.lock:
            self.mem.append(e)
            with open(self.path, "a") as f:
                f.write(json.dumps(e) + "\n")

    def events(self, run):
        with self.lock:
            return [e for e in self.mem if e["run"] == run]


def make_recorder():
    return Recorder()
