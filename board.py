"""The board: shared state for the fixer swarm (see CONTRACTS.md section 1).

Every peer reads and writes the board; nothing else connects them. LocalBoard
is a directory. ArchilBoard (board_archil.py, one Archil disk) has the same
methods; both build snapshot() with build_snapshot() below.
"""
import fcntl
import json
import os
import shutil
import threading
import time
from pathlib import Path

DEFAULT_ROOT = Path(__file__).parent / ".airlock-board"
_LOG = "log.jsonl"
_MEMORY = "memory"


class LocalBoard:
    kind = "local"

    def __init__(self, root=DEFAULT_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _p(self, path):
        return self.root / path

    def put(self, path, obj):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, p)

    def get(self, path):
        try:
            return json.loads(self._p(path).read_text())
        except FileNotFoundError:
            return None

    def exists(self, path):
        return self._p(path).exists()

    def list(self, prefix):
        base = self._p(prefix)
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*")
                      if p.is_file() and ".tmp" not in p.name)

    def delete(self, path):
        try:
            self._p(path).unlink()
        except FileNotFoundError:
            pass

    def claim(self, src, dst):
        d = self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(self._p(src), d)
            return True
        except FileNotFoundError:
            return False

    def log(self, agent, event, **data):
        # Peers run as separate processes, so the thread lock alone can't stop two
        # of them reading the same length and reusing a seq. flock the log itself.
        with self._lock, open(self._p(_LOG), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                seq = len(self.read_log())
                line = {"seq": seq, "ts": time.time(), "agent": agent, "event": event, **data}
                f.write(json.dumps(line) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def read_log(self, since=0):
        try:
            lines = self._p(_LOG).read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(l) for l in lines[since:] if l.strip()]

    def remember(self, kind, obj):
        p = self._p(f"{_MEMORY}/{kind}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(obj) + "\n")

    def recall(self, kind):
        try:
            lines = self._p(f"{_MEMORY}/{kind}.jsonl").read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(l) for l in lines if l.strip()]

    def reset_run(self):
        for child in self.root.iterdir():
            if child.name == _MEMORY:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def snapshot(self):
        return build_snapshot(self)


def build_snapshot(board):
    """CONTRACTS.md section 6. Shared by every backend so shapes are identical."""
    names = lambda d: [p.rsplit("/", 1)[-1] for p in board.list(f"tasks/{d}")]
    recs = lambda prefix: [r for r in (board.get(p) for p in board.list(prefix)) if r is not None]
    nodes, versions, evals = recs("nodes"), recs("versions"), recs("evals")
    return {
        "control": {"approved": board.get("control/approved.json"),
                    "done": board.get("control/done.json")},
        "scan": board.get("scan/results.json") or {},
        "nodes": nodes, "versions": versions, "evals": evals,
        "tasks": {d: names(d) for d in ("open", "claimed", "done")},
        "champion": champion(versions, evals, len(nodes)),
        "memory": {k: len(board.recall(k)) for k in ("fixes", "attacks")},
        "log_len": len(board.read_log()),
    }


def champion(versions, evals, node_count):
    """CONTRACTS.md section 2: best fresh, useful code-track version (or v0)."""
    by_id = {e["version"]: e for e in evals}
    best = None
    for v in versions:
        e = by_id.get(v["id"])
        if v["track"] == "policy" or not e or not e.get("utility") \
                or e.get("nodes_seen", 0) < node_count:
            continue
        key = (-e["passes"], len(v.get("patch", "")), v["generation"])
        if best is None or key < best[0]:
            best = (key, v["id"])
    return best[1] if best else None


def open_board(kind=None, **kw):
    kind = kind or os.environ.get("AIRLOCK_BOARD", "local")
    if kind == "local":
        return LocalBoard(**kw)
    if kind == "archil":
        from board_archil import ArchilBoard
        return ArchilBoard(**kw)
    raise ValueError(f"unknown board kind: {kind}")
