"""Append-only event log per run (the audit trail): JSONL + memory. Clones run
in parallel threads, so writes are locked. If an evidence locker is attached,
each clone's trail and anything it sent out are filed there too."""
import json
import threading
import time
from pathlib import Path

LOG = Path(__file__).parent / "audit.jsonl"


class Recorder:
    def __init__(self, path=LOG, locker=None):
        self.path = Path(path)
        self.mem = []
        self.lock = threading.Lock()
        self.locker = locker

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

    # ---- evidence locker ------------------------------------------------
    def capture_outbound(self, run, url, data):
        """The network 'went through': file exactly what left the machine."""
        if self.locker:
            self.locker.put(f"outbox/{run}.txt", f"POST {url}\n{data}")

    def file_run(self, run, verdict):
        """One clone finished: store its trail and add it to the shared ledger."""
        if not self.locker:
            return
        evs = self.events(run)
        self.locker.put(f"events/{run}.jsonl", "".join(json.dumps(e) + "\n" for e in evs))
        self.locker.append("ledger.jsonl", json.dumps(
            {"run": run, "score": verdict["score"], "verdict": verdict["verdict"],
             "reason": verdict["reason"]}) + "\n")


def make_recorder(locker=None):
    return Recorder(locker=locker)
