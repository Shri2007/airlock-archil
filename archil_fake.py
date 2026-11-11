"""In-memory fake of the Archil SDK pieces ArchilBoard uses, for tests.

FakeArchil mimics the `archil` module surface: list_disks, create_disk,
ExecMountSpec, exec. FakeDisk mimics Disk object calls: put_object, get_object,
list_objects, append_object, delete_object, delete_objects. Appends can be made
to fail with HTTP 409 (fail_appends=N, or racy=True to 409 whenever another
append is in flight, like the real service). exec understands the
`mkdir -p X && mv S D && echo TOKEN` script ArchilBoard sends; the mv is atomic.
"""
from __future__ import annotations

import shlex
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional


class FakeS3Error(Exception):
    def __init__(self, operation, status, code=""):
        super().__init__(f"S3 {operation} failed: {status} {code}", status, code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ExecMountSpec:
    disk: object
    subdirectory: Optional[str] = None
    read_only: bool = False
    conditional: bool = False


class FakeDisk:
    def __init__(self, name, fail_appends=0, racy=False):
        self.name = name
        self.id = f"dsk-fake-{name}"
        self.objects: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.fail_appends = fail_appends
        self.racy = racy
        self.append_409s = 0
        self._appending = threading.Lock()

    def put_object(self, key, body, content_type=None, **_):
        with self.lock:
            self.objects[key] = bytes(body)

    def get_object(self, key):
        with self.lock:
            if key not in self.objects:
                raise FakeS3Error("GetObject", 404, "NoSuchKey")
            return self.objects[key]

    def list_objects(self, prefix=None, *, recursive=False, **_):
        with self.lock:
            keys = sorted(k for k in self.objects if not prefix or k.startswith(prefix))
        return SimpleNamespace(objects=[SimpleNamespace(key=k, size=0) for k in keys],
                               common_prefixes=[], is_truncated=False, key_count=len(keys))

    def append_object(self, key, body, content_type=None, **_):
        with self.lock:
            if self.fail_appends > 0:
                self.fail_appends -= 1
                self.append_409s += 1
                raise FakeS3Error("AppendObject", 409, "Conflict")
        if not self._appending.acquire(blocking=not self.racy):
            with self.lock:
                self.append_409s += 1
            raise FakeS3Error("AppendObject", 409, "Conflict")
        try:
            if self.racy:
                time.sleep(0.001)
            with self.lock:
                self.objects[key] = self.objects.get(key, b"") + bytes(body)
        finally:
            self._appending.release()

    def delete_object(self, key):
        with self.lock:
            self.objects.pop(key, None)

    def delete_objects(self, keys, *, quiet=False):
        with self.lock:
            for k in keys:
                self.objects.pop(k, None)

    # ---- used by fake exec ----
    def _mv(self, src, dst):
        with self.lock:
            if src not in self.objects:
                return False
            self.objects[dst] = self.objects.pop(src)
            return True


class FakeArchil:
    ExecMountSpec = ExecMountSpec

    def __init__(self, **disk_kw):
        self.disks: dict[str, FakeDisk] = {}
        self.disk_kw = disk_kw
        self.execs = 0

    def list_disks(self, *, name=None, **_):
        return [d for n, d in self.disks.items() if name is None or n == name]

    def create_disk(self, *, name, **_):
        d = self.disks[name] = FakeDisk(name, **self.disk_kw)
        return SimpleNamespace(disk=d)

    def exec(self, *, disks, command, **_):
        self.execs += 1
        mounts = {f"/mnt/archil/{k}/": (v.disk if isinstance(v, ExecMountSpec) else v)
                  for k, v in disks.items()}

        def resolve(path):
            for root, disk in mounts.items():
                if path.startswith(root):
                    return disk, path[len(root):]
            raise ValueError(f"path outside mounts: {path}")

        out = []
        for part in command.split(" && "):
            argv = shlex.split(part)
            if argv[0] == "mkdir":
                continue
            if argv[0] == "echo":
                out.append(" ".join(argv[1:]))
            elif argv[0] == "mv":
                (d1, s), (d2, t) = resolve(argv[1]), resolve(argv[2])
                if d1 is not d2 or not d1._mv(s, t):
                    return SimpleNamespace(exit_code=1, stdout="\n".join(out),
                                           stderr=f"mv: cannot stat '{argv[1]}'")
            else:
                return SimpleNamespace(exit_code=127, stdout="", stderr=f"{argv[0]}: not found")
        return SimpleNamespace(exit_code=0, stdout="\n".join(out) + "\n", stderr="")
