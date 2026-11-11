"""ArchilBoard: the board on one Archil disk (CONTRACTS.md section 1).

Same API as board.LocalBoard. Objects go through the disk's S3-style API;
claims are an atomic `mv` inside a serverless exec with the disk mounted
(strongly consistent), so exactly one racing caller wins.

Archil rejects racing appends with HTTP 409, so appends are serialized per
process and retried with backoff (same pattern as evidence.ArchilLocker).
Log `seq` is the line index, assigned on read, so it stays correct even when
several processes append to the same log.

`client` is the archil module by default; tests pass archil_fake.FakeArchil().
"""
from __future__ import annotations

import json
import shlex
import threading
import time
import uuid

from board import build_snapshot

DISK_NAME = "airlock-board"
MOUNT = "/mnt/archil/b"
_LOG = "log.jsonl"
_MEMORY = "memory"
_RETRIES = 8


def _status(e):
    s = getattr(e, "status", None) or getattr(e, "status_code", None)
    if s is None and e.args and isinstance(e.args[-1], int):
        s = e.args[-1]
    return s


def _real_client():
    from workspace import _archil_setup
    _archil_setup()
    import archil
    return archil


class ArchilBoard:
    kind = "archil"

    def __init__(self, disk_name=DISK_NAME, client=None):
        self.client = client or _real_client()
        found = self.client.list_disks(name=disk_name)
        self.disk = found[0] if found else self.client.create_disk(name=disk_name).disk
        self.disk_name = disk_name
        self._append_lock = threading.Lock()
        self.root = f"archil:{disk_name}"

    # ---- objects ----------------------------------------------------------
    @staticmethod
    def _k(path):
        return path.lstrip("/")

    def put(self, path, obj):
        self.disk.put_object(self._k(path), json.dumps(obj, indent=2).encode(),
                             "application/json")

    def _get_bytes(self, path):
        try:
            return self.disk.get_object(self._k(path))
        except Exception as e:
            if _status(e) == 404:
                return None
            raise

    def get(self, path):
        raw = self._get_bytes(path)
        return None if raw is None else json.loads(raw.decode())

    def exists(self, path):
        return self._get_bytes(path) is not None

    def list(self, prefix):
        p = self._k(prefix).rstrip("/")
        res = self.disk.list_objects((p + "/") if p else None, recursive=True)
        keys = (o.key.lstrip("/") for o in res.objects)
        return sorted(k for k in keys if not k.endswith("/"))

    def delete(self, path):
        try:
            self.disk.delete_object(self._k(path))
        except Exception as e:
            if _status(e) != 404:
                raise

    # ---- claims -----------------------------------------------------------
    def claim(self, src, dst):
        """Atomic rename on the mounted disk. True iff THIS caller's mv won."""
        token = f"CLAIMED-{uuid.uuid4().hex}"
        s, d = f"{MOUNT}/{self._k(src)}", f"{MOUNT}/{self._k(dst)}"
        ddir = d.rsplit("/", 1)[0]
        cmd = (f"mkdir -p {shlex.quote(ddir)} && mv {shlex.quote(s)} {shlex.quote(d)}"
               f" && echo {token}")
        spec = self.client.ExecMountSpec(self.disk, conditional=True)
        try:
            r = self.client.exec(disks={"b": spec}, command=cmd)
        except Exception:
            return False
        return r.exit_code == 0 and token in (r.stdout or "")

    # ---- appends ----------------------------------------------------------
    def _append(self, path, line):
        with self._append_lock:
            for attempt in range(_RETRIES):
                try:
                    self.disk.append_object(self._k(path), line.encode(), "text/plain")
                    return
                except Exception as e:
                    if _status(e) != 409 and "409" not in str(e) or attempt == _RETRIES - 1:
                        raise
                    time.sleep(0.05 * (2 ** attempt))

    def _lines(self, path):
        raw = self._get_bytes(path)
        if raw is None:
            return []
        return [json.loads(l) for l in raw.decode().splitlines() if l.strip()]

    def log(self, agent, event, **data):
        # seq written best-effort; read_log() re-derives it from line order.
        line = {"seq": len(self._lines(_LOG)), "ts": time.time(), "agent": agent,
                "event": event, **data}
        self._append(_LOG, json.dumps(line) + "\n")

    def read_log(self, since=0):
        out = []
        for i, e in enumerate(self._lines(_LOG)):
            if i >= since:
                e["seq"] = i
                out.append(e)
        return out

    def remember(self, kind, obj):
        self._append(f"{_MEMORY}/{kind}.jsonl", json.dumps(obj) + "\n")

    def recall(self, kind):
        return self._lines(f"{_MEMORY}/{kind}.jsonl")

    # ---- run lifecycle ----------------------------------------------------
    def reset_run(self):
        res = self.disk.list_objects(None, recursive=True)
        keys = [o.key.lstrip("/") for o in res.objects]
        doomed = [k for k in keys if not k.startswith(_MEMORY + "/") and not k.endswith("/")]
        for i in range(0, len(doomed), 500):
            self.disk.delete_objects(doomed[i:i + 500], quiet=True)

    def snapshot(self):
        return build_snapshot(self)
