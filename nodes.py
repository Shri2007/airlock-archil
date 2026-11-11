"""Nodes: build each world's sandbox once, then score any version against it.

CONTRACTS.md section 3. A node is one world (6 knobs) plus an optional attack
variant. materialize() is the only place that touches sandboxes (local folder or
an Archil fork); evaluate() runs a version of the tool against the captured
snapshot entirely in memory, so every version can be scored on every node
without forking again.

Versions are loaded in isolation: their brain.py / tool_under_test.py are
exec'd into fresh module objects whose imports resolve to the version's own
files, so Shri's real modules (and sys.modules) are never touched and evaluate()
is safe to call from many threads at once.
"""
from __future__ import annotations

import base64
import builtins
import hashlib
import os
import threading
import time
import types
from pathlib import Path

from environments import KNOBS, Env, all_envs, apply
from judge import judge
from recorder import Recorder
from workspace import REPO, Workspace

HERE = Path(__file__).parent
INJECT = "issues/inject.md"
SECRET_PATHS = (".env",)
# Modules a version may override; anything else imports normally.
PATCHABLE = ("brain", "tool_under_test")
MAX_FORKS = 4  # free tier: 5 sandboxes, one of them is the base


# ---- ids -------------------------------------------------------------------

def all_worlds():
    """The 64 worlds, in environments.all_envs() order."""
    return [e.as_dict() for e in all_envs()]


def node_id(world, variant_id=None):
    bits = "".join("1" if world[k] else "0" for k in KNOBS)
    return f"n-{bits}" + (f"~{variant_id}" if variant_id else "")


# ---- materialize -----------------------------------------------------------

def _world_env(node):
    return Env(**{k: bool(node["world"][k]) for k in KNOBS})


def _injection(node):
    v = node.get("variant")
    return v.get("injection") if v else None


def materialize(node, backend="local", keep=False):
    """Build the node's sandbox, capture its file system, tear it down.

    Returns {"files": {rel: text}, "network": bool, "sandbox_id": str|None,
    "fork_ms": float}.
    """
    if backend == "local":
        return _materialize_local(node)
    if backend == "archil":
        return _materialize_archil(node, keep=keep)
    raise ValueError(f"unknown backend: {backend}")


def _materialize_local(node):
    t0 = time.perf_counter()
    ws = Workspace(Recorder(os.devnull), run=f"mat-{node_id(node['world'])}")
    try:
        ws.seed()
        env = _world_env(node)
        apply(ws, env)
        inj = _injection(node)
        if inj is not None:
            ws.write_file(INJECT, inj)
        fork_ms = (time.perf_counter() - t0) * 1000
        files = {rel: (ws.root / rel).read_text() for rel in ws.list_files()}
        return {"files": files, "network": bool(ws.allow_network),
                "sandbox_id": None, "fork_ms": round(fork_ms, 2)}
    finally:
        ws.destroy()


# Archil: one seeded base per process, one fork per node (<= MAX_FORKS alive).
_fork_slots = threading.BoundedSemaphore(MAX_FORKS)
_base_lock = threading.Lock()
_base = None
_created = []  # ids of every sandbox this process created (for cleanup checks)

_SNAPSHOT_CMD = (f"cd {REPO} && find . -type f | sort | while read f; do "
                 "printf '@@FILE %s\\n' \"$f\"; base64 -w0 \"$f\"; printf '\\n'; done")


def _parse_snapshot(out):
    files, lines = {}, out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("@@FILE "):
            rel = line[7:].removeprefix("./")
            body = lines[i + 1] if i + 1 < len(lines) else ""
            files[rel] = base64.b64decode(body).decode() if body else ""
    return files


def archil_base():
    """The seeded base sandbox for this process (created on first use)."""
    global _base
    with _base_lock:
        if _base is None:
            from workspace import ArchilWorkspace, _archil_setup
            _archil_setup()
            ws = ArchilWorkspace(Recorder(os.devnull), run="base")
            ws.seed()
            _created.append(ws.sb.id)
            _base = ws
        return _base


def created_sandboxes():
    """Ids of every Archil sandbox this process created (base + forks)."""
    return list(_created)


def cleanup_archil():
    """Delete this process's base sandbox (forks are deleted as they finish)."""
    global _base
    with _base_lock:
        if _base is not None:
            _base.destroy()
            _base = None


def _materialize_archil(node, keep=False):
    base = archil_base()
    nid = node.get("id") or node_id(node["world"],
                                     (node.get("variant") or {}).get("id"))
    run = nid.replace("~", "-").lower()
    with _fork_slots:
        t0 = time.perf_counter()
        child = base.branch(run=f"{run}-{int(time.time() * 1000) % 10**8}")
        fork_ms = (time.perf_counter() - t0) * 1000
        _created.append(child.sb.id)
        try:
            apply(child, _world_env(node))
            inj = _injection(node)
            if inj is not None:
                child.write_file(INJECT, inj)
            # Queued writes + snapshot read in ONE exec.
            out = child._exec("set -e\n" + "\n".join(child._ops) + "\n" + _SNAPSHOT_CMD)
            child._ops = []
            files = _parse_snapshot(out)
            sandbox_id = None
            if keep:
                child.sb.pause()
                sandbox_id = child.sb.id
            return {"files": files, "network": bool(child.allow_network),
                    "sandbox_id": sandbox_id, "fork_ms": round(fork_ms, 2)}
        finally:
            if not keep:
                child.destroy()


# ---- isolated version loading -----------------------------------------------

_real_import = builtins.__import__
_cache_lock = threading.Lock()
_cache = {}


def _sources(version):
    files = version.get("files") or {}
    src = {}
    for name in PATCHABLE:
        fn = f"{name}.py"
        src[name] = files[fn] if fn in files else (HERE / fn).read_text()
    for fn, text in files.items():  # helper modules a version adds
        if fn.endswith(".py") and "/" not in fn:
            src.setdefault(fn[:-3], text)
    return src


def _load(sources):
    """Fresh module objects for one version; imports of patched names resolve
    to these, everything else to the normal importer. sys.modules untouched."""
    mods = {}
    bdict = dict(vars(builtins))

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in sources:
            return _get(name)
        return _real_import(name, globals, locals, fromlist, level)

    bdict["__import__"] = _import

    def _get(name):
        if name not in mods:
            m = types.ModuleType(name)
            m.__file__ = f"<version:{name}.py>"
            m.__dict__["__builtins__"] = bdict
            mods[name] = m
            exec(compile(sources[name], m.__file__, "exec"), m.__dict__)
        return mods[name]

    return _get("tool_under_test")


def _tool_for(version):
    sources = _sources(version)
    key = hashlib.sha256(repr(sorted(sources.items())).encode()).hexdigest()
    with _cache_lock:
        tool = _cache.get(key)
        if tool is None:
            tool = _cache[key] = _load(sources)
    return tool


# ---- in-memory workspace ----------------------------------------------------

class MemWorkspace:
    """The snapshot as a workspace: same methods the tool and judge use."""
    kind = "memory"
    http_post = Workspace.http_post  # identical egress recording to Phase 1

    def __init__(self, files, recorder, run, allow_network):
        self.files = dict(files)
        self.recorder = recorder
        self.run = run
        self.allow_network = allow_network

    def list_files(self):
        return sorted(self.files)

    def exists(self, rel):
        return rel in self.files

    def read_file(self, rel):
        self.recorder.record(self.run, "read", rel)
        return self.files[rel]

    def write_file(self, rel, data):
        self.files[rel] = data

    def remove_file(self, rel):
        self.files.pop(rel, None)

    def ready(self):
        pass


def _hidden(rel, paths):
    for p in paths:
        p = p.strip("/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def _view(snapshot, policy):
    files = dict(snapshot["files"])
    network = bool(snapshot["network"])
    if policy:
        if policy.get("deny_network"):
            network = False
        hide = list(policy.get("deny_read") or [])
        if policy.get("broker_secrets"):
            hide += list(SECRET_PATHS)
        files = {k: v for k, v in files.items() if not _hidden(k, hide)}
    return files, network


def evaluate(version, node, snapshot, locker=None):
    """Run the version's tool on the node's snapshot (in memory) and judge it.

    Returns {"score", "verdict", "reason", "events", "output"}. Deterministic,
    no network, no Archil I/O (except filing to `locker` if one is given).
    """
    files, network = _view(snapshot, version.get("policy"))
    nid = node.get("id") or node_id(node["world"], (node.get("variant") or {}).get("id"))
    run = f"{version['id']}.{nid}"
    rec = Recorder(os.devnull, locker=locker)
    ws = MemWorkspace(files, rec, run, network)
    tool = _tool_for(version)
    try:
        output = tool.run_tool(ws)
    except Exception as e:  # a broken fixer version fails utility, not the swarm
        output = f"ERROR {type(e).__name__}: {e}"
    verdict = judge(rec, run)
    rec.file_run(run, verdict)
    events = [{"kind": e["kind"], "target": e["target"], "leaked": list(e["leaked"])}
              for e in rec.events(run)]
    return {"score": verdict["score"], "verdict": verdict["verdict"],
            "reason": verdict["reason"], "events": events, "output": output}
