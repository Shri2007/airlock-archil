"""The environment TWIN. Two backends, same methods:

  Workspace         a throwaway local folder (default, free, instant)
  ArchilWorkspace   a real Archil sandbox (microVM); branch() = sandbox.fork()

USE_ARCHIL=1 switches. Each Archil exec costs ~1.5s, so ArchilWorkspace batches:
file writes are queued and sent as ONE exec, and the tool's reads come from ONE
snapshot exec of the repo.
"""
from __future__ import annotations

import base64
import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from tags import tags_in

SEED = Path(__file__).parent / "twin"
REPO = "/repo"


class Workspace:
    kind = "local folder"

    def __init__(self, recorder, run, root=None, allow_network=True):
        self.recorder = recorder
        self.run = run
        self.allow_network = allow_network
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="twin_"))

    def seed(self):
        shutil.copytree(SEED, self.root, dirs_exist_ok=True)

    def list_files(self):
        return sorted(str(p.relative_to(self.root))
                      for p in self.root.rglob("*") if p.is_file())

    def exists(self, rel):
        return (self.root / rel).exists()

    def read_file(self, rel):
        self.recorder.record(self.run, "read", rel)
        return (self.root / rel).read_text()

    def write_file(self, rel, data):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)

    def remove_file(self, rel):
        p = self.root / rel
        if p.exists():
            p.unlink()

    def http_post(self, url, data):
        # Never actually sent anywhere: we only record the attempt.
        leaked = tags_in(data)
        kind = "egress" if self.allow_network else "egress-blocked"
        self.recorder.record(self.run, kind, url, leaked=leaked)

    def ready(self):
        """Hook for backends that batch work; nothing to do locally."""

    def branch(self, run):
        forked = Path(tempfile.mkdtemp(prefix="branch_"))
        shutil.copytree(self.root, forked, dirs_exist_ok=True)
        return Workspace(self.recorder, run, root=forked,
                         allow_network=self.allow_network)

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)


class ArchilWorkspace(Workspace):
    kind = "archil sandbox"

    def __init__(self, recorder, run, sandbox=None, allow_network=True):
        self.recorder = recorder
        self.run = run
        self.allow_network = allow_network
        self.sb = sandbox
        self._ops = []      # queued shell ops, flushed as one exec
        self._files = None  # cached snapshot: rel path -> text

    # ---- batching -------------------------------------------------------
    def _exec(self, script):
        r = self.sb.exec(script)
        if r.exit_code != 0:
            raise RuntimeError(f"[{self.run}] exec failed ({r.exit_code}): {r.stderr[:300]}")
        return r.stdout

    def ready(self):
        """Send all queued writes/removes in a single exec."""
        if self._ops:
            self._exec("set -e\n" + "\n".join(self._ops))
            self._ops = []
            self._files = None

    def _snapshot(self):
        self.ready()
        if self._files is None:
            out = self._exec(
                f"cd {REPO} && find . -type f | sort | while read f; do "
                "printf '@@FILE %s\\n' \"$f\"; base64 -w0 \"$f\"; printf '\\n'; done")
            files, lines = {}, out.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("@@FILE "):
                    rel = line[7:].removeprefix("./")
                    body = lines[i + 1] if i + 1 < len(lines) else ""
                    files[rel] = base64.b64decode(body).decode() if body else ""
            self._files = files
        return self._files

    # ---- same interface as Workspace -----------------------------------
    def seed(self):
        import archil
        self.sb = archil.create_sandbox(name=f"airlock-base-{int(time.time())}",
                                        base_image="ubuntu:24.04", max_ttl_seconds=1500)
        self._ops.append(f"mkdir -p {REPO}")
        for p in SEED.rglob("*"):
            if p.is_file():
                self.write_file(str(p.relative_to(SEED)), p.read_text())
        self.ready()

    def list_files(self):
        return sorted(self._snapshot())

    def exists(self, rel):
        return rel in self._snapshot()

    def read_file(self, rel):
        self.recorder.record(self.run, "read", rel)
        return self._snapshot()[rel]

    def write_file(self, rel, data):
        path = shlex.quote(f"{REPO}/{rel}")
        b64 = base64.b64encode(data.encode()).decode()
        self._ops.append(f"mkdir -p \"$(dirname {path})\" && echo {b64} | base64 -d > {path}")

    def remove_file(self, rel):
        self._ops.append(f"rm -f {shlex.quote(f'{REPO}/{rel}')}")

    def branch(self, run):
        self.ready()
        return ArchilWorkspace(self.recorder, run, sandbox=self.sb.fork(name=f"airlock-{run}"),
                               allow_network=self.allow_network)

    def destroy(self):
        if self.sb is None:
            return
        try:
            self.sb.delete()
        except Exception:
            try:
                self.sb.stop()
                self.sb.delete()
            except Exception as e:
                print(f"  ! could not delete sandbox for {self.run}: {e}")


def _archil_setup():
    # python.org Python on macOS ships without CA certs; Archil's websocket needs them.
    if "SSL_CERT_FILE" not in os.environ:
        try:
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass
    if not os.environ.get("ARCHIL_API_KEY"):
        raise SystemExit("USE_ARCHIL=1 but ARCHIL_API_KEY is not set (check .env)")


def using_archil():
    return os.environ.get("USE_ARCHIL") == "1"


def make_workspace(recorder, run):
    if using_archil():
        _archil_setup()
        return ArchilWorkspace(recorder, run)
    return Workspace(recorder, run)
