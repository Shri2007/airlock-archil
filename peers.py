"""The swarm: five roles that only talk through the board (CONTRACTS.md section 4).

    python3 peers.py <role> [--board local|archil] [--backend local|archil] [--n 1]

Roles: scout, lab, fixer-code, fixer-policy, attacker. Each is a class with
step(board) -> bool (did it do any work). The same classes run as separate
processes (poll every 0.5 s) or inside run_scheduler(), a seeded round-robin in
one process that tests and `swarm.py up --deterministic` use. Roles never call
each other: they claim tasks on the board with an atomic rename and post new
tasks for whoever picks them up.
"""
import argparse
import json
import os
import random
import time
from pathlib import Path

import catalog
from board import champion as pick_champion
from board import open_board

HERE = Path(__file__).parent
MAX_GENERATION = 3
MAX_ARCHIL_FORKS = 80
POLL_S = 0.5
QUIET_POLLS = 3
ROLES = ("scout", "lab", "fixer-code", "fixer-policy", "attacker")


# ---------------------------------------------------------------- task helpers

def _base(path):
    return path.rsplit("/", 1)[-1][:-len(".json")]


def task_name(path):
    """tasks/claimed/eval.v1.a.lab-1.json -> eval.v1.a (open: no agent suffix)."""
    b = _base(path)
    return b if path.startswith("tasks/open/") else b.rsplit(".", 1)[0]


def open_tasks(board, kind):
    return [_base(p) for p in board.list("tasks/open") if _base(p).split(".", 1)[0] == kind]


def task_known(board, name):
    if board.exists(f"tasks/open/{name}.json"):
        return True
    return any(task_name(p) == name for d in ("claimed", "done")
               for p in board.list(f"tasks/{d}"))


def post(board, name, **body):
    """Post a task unless it was ever posted this run. True if posted."""
    if task_known(board, name):
        return False
    kind, subject = name.split(".", 1)
    board.put(f"tasks/open/{name}.json", {"kind": kind, "subject": subject,
                                          "ts": time.time(), **body})
    return True


def approved(board):
    return board.exists("control/approved.json")


def load_all(board, prefix):
    return [r for r in (board.get(p) for p in board.list(prefix)) if r]


def pending_nodes(board):
    return [n["id"] for n in load_all(board, "nodes") if n.get("state") != "ready"]


def quiescent(board):
    """Nothing left to do: scout has run, no open or claimed tasks, nodes ready."""
    return (board.exists("versions/v0.json") and not board.list("tasks/open")
            and not board.list("tasks/claimed") and not pending_nodes(board))


def write_done(board, by, reason):
    if board.exists("control/done.json"):
        return False
    board.put("control/done.json", {"by": by, "ts": time.time(), "reason": reason})
    board.log(by, "done", reason=reason)
    return True


def _next_code_id(board, gen):
    used = {v["id"] for v in load_all(board, "versions")}
    for letter in "abcdefghijklmnopqrstuvwxyz":
        if f"v{gen}.{letter}" not in used:
            return f"v{gen}.{letter}"
    raise RuntimeError("out of version letters")


def _next_policy_id(board, gen):
    used = {v["id"] for v in load_all(board, "versions")}
    n = 1
    while f"v{gen}.p{n}" in used:
        n += 1
    return f"v{gen}.p{n}"


# ----------------------------------------------------------------------- roles

class Role:
    role = "?"

    def __init__(self, n=1, backend="local"):
        self.name = f"{self.role}#{n}"
        self.file = self.name.replace("#", "-")
        self.backend = backend

    def claim(self, board, name, quiet=False):
        ok = board.claim(f"tasks/open/{name}.json", f"tasks/claimed/{name}.{self.file}.json")
        if ok and not quiet:  # node claims are implied by the node event
            board.log(self.name, "claimed", task=name)
        return ok

    def task(self, board, name):
        return board.get(f"tasks/claimed/{name}.{self.file}.json") or {}

    def finish(self, board, name):
        board.claim(f"tasks/claimed/{name}.{self.file}.json", f"tasks/done/{name}.{self.file}.json")

    def step(self, board):
        raise NotImplementedError


class Scout(Role):
    """Seeds the run: Phase 1 scan, v0, the 64 grid nodes, and the first tasks."""
    role = "scout"

    def step(self, board):
        if board.exists("versions/v0.json"):
            return False
        src = HERE / "results.json"
        if not src.exists():
            src = HERE / "fixtures" / "results.scan.json"
        scan = json.loads(src.read_text())
        import brain
        import nodes
        # The tool profile every version here is built on (brain.PROFILES); the
        # copied Phase 1 scan may have come from a different one.
        scan["swarm_profile"] = brain.profile()
        board.put("scan/results.json", scan)
        ids = []
        for world in nodes.all_worlds():
            nid = nodes.node_id(world)
            board.put(f"nodes/{nid}.json", {
                "id": nid, "world": world, "variant": None, "origin": "grid",
                "added_by": self.name, "against": None, "backend": None,
                "sandbox_id": None, "fork_ms": 0.0, "state": "pending"})
            post(board, f"node.{nid}")
            ids.append(nid)
        v0 = {"id": "v0", "parent": None, "generation": 0, "track": "base",
              "agent": self.name, "source": "seed", "title": "Original tool",
              "rationale": "Shri's summarize_issues() as written.",
              "files": {}, "patch": "", "policy": None, "fixes": []}
        board.put("versions/v0.json", v0)
        board.log(self.name, "version", id="v0", parent=None, track="base",
                  generation=0, title=v0["title"])
        board.log(self.name, "node", count=len(ids))
        post(board, "eval.v0", priority=0)
        return True


class Lab(Role):
    """The only role that builds sandboxes (materialize) and scores versions."""
    role = "lab"

    def __init__(self, n=1, backend="local"):
        super().__init__(n, backend)
        self._snaps = {}
        self._v0_out = {}

    # -- nodes --
    def _materialize(self, board, name):
        import nodes
        nid = name.split(".", 1)[1]
        node = board.get(f"nodes/{nid}.json")
        backend = self.backend
        if backend == "archil":
            forks = sum(1 for n in load_all(board, "nodes") if n.get("backend") == "archil")
            if forks >= MAX_ARCHIL_FORKS:
                board.log(self.name, "error", msg=f"archil fork cap {MAX_ARCHIL_FORKS} hit; "
                          f"{nid} built locally")
                backend = "local"
        m = nodes.materialize(node, backend)
        board.put(f"snapshots/{nid}.json", {"files": m["files"], "network": m["network"]})
        node.update(backend=backend, sandbox_id=m["sandbox_id"], fork_ms=m["fork_ms"],
                    state="ready")
        board.put(f"nodes/{nid}.json", node)
        board.log(self.name, "node", id=nid, fork_ms=m["fork_ms"], backend=backend,
                  sandbox_id=m["sandbox_id"])

    def _snap(self, board, nid):
        if nid not in self._snaps:
            snap = board.get(f"snapshots/{nid}.json")
            if snap is None:
                return None  # not built yet: don't cache the miss
            self._snaps[nid] = snap
        return self._snaps[nid]

    # -- evals --
    def _score(self, board, name):
        import nodes
        vid = name.split(".", 1)[1].split("@", 1)[0]
        version = board.get(f"versions/{vid}.json")
        all_nodes = load_all(board, "nodes")
        prev = board.get(f"evals/{vid}.json")
        if version is None or (prev and prev["nodes_seen"] >= len(all_nodes)):
            return  # already fresh (duplicate re-eval request)
        # The attacker may have added nodes since step() checked readiness:
        # score nothing until every node has a snapshot, and let step() requeue.
        snaps = {n["id"]: self._snap(board, n["id"]) for n in all_nodes}
        if any(n.get("state") != "ready" or snaps[n["id"]] is None for n in all_nodes):
            return "defer"
        v0 = board.get("versions/v0.json")
        results, utility = {}, True
        for node in all_nodes:
            snap = snaps[node["id"]]
            r = nodes.evaluate(version, node, snap)
            results[node["id"]] = r
            if not node["world"]["injected"] and vid != "v0":
                key = node["id"]
                if key not in self._v0_out:
                    self._v0_out[key] = nodes.evaluate(v0, node, snap)["output"]
                utility = utility and r["output"] == self._v0_out[key]
        passes = sum(1 for r in results.values() if r["verdict"] == "PASS")
        total = len(all_nodes)
        backends = {n.get("backend") for n in all_nodes}
        board.put(f"evals/{vid}.json", {
            "version": vid, "passes": passes, "total": total, "utility": utility,
            "nodes_seen": total, "backend": "archil" if "archil" in backends else "local",
            "ts": time.time(), "results": results})
        board.log(self.name, "eval", version=vid, passes=passes, total=total, utility=utility)
        self._remember_attacks(board, version, all_nodes, results)
        self._champion(board, total)
        self._follow_up(board, version, passes, total, utility)

    def _remember_attacks(self, board, version, all_nodes, results):
        seen = {(m.get("node"), (m.get("variant") or {}).get("injection"))
                for m in board.recall("attacks")}
        for node in all_nodes:
            if node["origin"] != "attack" or node["against"] != version["id"]:
                continue
            if results[node["id"]]["verdict"] != "BLOCK":
                continue
            key = (node["id"], node["variant"]["injection"])
            if key not in seen:
                board.remember("attacks", {"node": node["id"], "world": node["world"],
                                           "variant": node["variant"],
                                           "against": version["id"],
                                           "against_patch": version.get("patch", "")})
                seen.add(key)

    def _champion(self, board, node_count):
        versions, evals = load_all(board, "versions"), load_all(board, "evals")
        best = pick_champion(versions, evals, node_count)
        last = next((e["version"] for e in reversed(board.read_log())
                     if e["event"] == "champion"), None)
        if best is None or best == last:
            return
        ev = next(e for e in evals if e["version"] == best)
        board.log(self.name, "champion", version=best, passes=ev["passes"], total=ev["total"])
        v = next(v for v in versions if v["id"] == best)
        if ev["passes"] == ev["total"] and v["track"] == "code":
            if all(m.get("patch") != v["patch"] for m in board.recall("fixes")):
                board.remember("fixes", {
                    "version": v["id"], "track": "code", "title": v["title"],
                    "rationale": v["rationale"], "files": v["files"],
                    "patch": v["patch"], "fixes": v.get("fixes", []),
                    "passes": ev["passes"], "total": ev["total"]})

    def _follow_up(self, board, version, passes, total, utility):
        vid, track = version["id"], version["track"]
        if track == "policy" or not utility:
            return
        if passes < total and version["generation"] < MAX_GENERATION:
            post(board, f"fix-code.{vid}")
            if vid == "v0":
                post(board, "fix-policy.v0")
        elif passes == total:
            post(board, f"attack.{vid}")

    def _stale(self, board):
        count = len(load_all(board, "nodes"))
        posted = False
        for ev in load_all(board, "evals"):
            vid = ev["version"]
            if ev["nodes_seen"] < count and not any(
                    task_name(p).startswith(f"eval.{vid}@") or task_name(p) == f"eval.{vid}"
                    for d in ("open", "claimed") for p in board.list(f"tasks/{d}")):
                posted |= post(board, f"eval.{vid}@{count}", priority=1)
        return posted

    def step(self, board):
        did = False
        # all_worlds() order: the all-knobs-on worlds first
        for name in sorted(open_tasks(board, "node"), reverse=True):
            if self.claim(board, name, quiet=True):
                self._materialize(board, name)
                self.finish(board, name)
                did = True
        if did or pending_nodes(board):
            return did
        names = [n for n in open_tasks(board, "eval") if n == "eval.v0" or approved(board)]
        prio = lambda n: ((board.get(f"tasks/open/{n}.json") or {}).get("priority", 1), n)
        for name in sorted(names, key=prio):
            if self.claim(board, name):
                if self._score(board, name) == "defer":  # new nodes appeared mid-step
                    board.claim(f"tasks/claimed/{name}.{self.file}.json", f"tasks/open/{name}.json")
                    break
                self.finish(board, name)  # _score posts follow-ups before this
                did = True
        if approved(board):
            did |= self._stale(board)
        return did


class FixerCode(Role):
    """Turns a failing version into up to 2 child versions (memory, then catalog)."""
    role = "fixer-code"

    def _candidates(self, board, parent):
        gen = parent["generation"] + 1
        mem = [m for m in board.recall("fixes") if m.get("track") == "code"
               and m.get("patch") and m["patch"] != parent.get("patch")]
        if mem:  # the fix that won last time, straight away
            m = mem[-1]
            return [{"title": m["title"], "source": "memory", "files": m["files"],
                     "fixes": m.get("fixes", []),
                     "rationale": f"From memory (won a previous run as {m['version']}): "
                                  + m["rationale"]}]
        out = []
        if catalog.claude_enabled():  # opt-in; None on any failure -> catalog only
            ev = board.get(f"evals/{parent['id']}.json") or {"results": {}}
            fails = []
            for nid, r in sorted(ev["results"].items()):
                if r["verdict"] == "BLOCK":
                    node = board.get(f"nodes/{nid}.json") or {}
                    snap = board.get(f"snapshots/{nid}.json") or {"files": {}}
                    fails.append({"node": nid, "reason": r["reason"],
                                  "injection": snap["files"].get("issues/inject.md"),
                                  "variant": (node.get("variant") or {}).get("id")})
            c = catalog.claude_fix(parent, fails)
            if c:
                out.append(c)
        for fix in catalog.fixes_for(gen):
            if fix["id"] in parent.get("fixes", []):
                continue
            out.append({"title": fix["title"], "source": "catalog",
                        "rationale": fix["rationale"],
                        "files": catalog.apply_fix(parent["files"], fix),
                        "fixes": parent.get("fixes", []) + [fix["id"]]})
        return out[:2]

    def step(self, board):
        if not approved(board):
            return False
        did = False
        for name in open_tasks(board, "fix-code"):
            if not self.claim(board, name):
                continue
            did = True
            parent = board.get(f"versions/{name.split('.', 1)[1]}.json")
            gen = parent["generation"] + 1
            made = []
            if gen <= MAX_GENERATION:
                patches = {v.get("patch") for v in load_all(board, "versions")}
                for c in self._candidates(board, parent):
                    patch = catalog.make_patch(c["files"])
                    if patch in patches:
                        continue
                    vid = _next_code_id(board, gen)
                    board.put(f"versions/{vid}.json", {
                        "id": vid, "parent": parent["id"], "generation": gen,
                        "track": "code", "agent": self.name, "source": c["source"],
                        "title": c["title"], "rationale": c["rationale"],
                        "files": c["files"], "patch": patch, "policy": None,
                        "fixes": c["fixes"]})
                    board.log(self.name, "version", id=vid, parent=parent["id"],
                              track="code", generation=gen, title=c["title"])
                    post(board, f"eval.{vid}", priority=0)
                    made.append(vid)
            if not made:
                board.log(self.name, "error", msg=f"no fix left for {parent['id']} "
                          f"(generation {gen}, cap {MAX_GENERATION})")
            self.finish(board, name)
        return did


class FixerPolicy(Role):
    """Hardens the sandbox instead of the code: policy versions run v0's code."""
    role = "fixer-policy"

    def step(self, board):
        if not approved(board):
            return False
        did = False
        for name in open_tasks(board, "fix-policy"):
            if not self.claim(board, name):
                continue
            did = True
            parent = board.get(f"versions/{name.split('.', 1)[1]}.json")
            gen = parent["generation"] + 1
            have = [v.get("policy") for v in load_all(board, "versions")]
            for fix in catalog.POLICY_FIXES:
                if fix["policy"] in have or gen > MAX_GENERATION:
                    continue
                vid = _next_policy_id(board, gen)
                board.put(f"versions/{vid}.json", {
                    "id": vid, "parent": parent["id"], "generation": gen,
                    "track": "policy", "agent": self.name, "source": "catalog",
                    "title": fix["title"], "rationale": fix["rationale"],
                    "files": {}, "patch": "", "policy": fix["policy"], "fixes": [fix["id"]]})
                board.log(self.name, "version", id=vid, parent=parent["id"],
                          track="policy", generation=gen, title=fix["title"])
                post(board, f"eval.{vid}", priority=0)
            self.finish(board, name)
        return did


class Attacker(Role):
    """Tries to beat a version that passes everything by adding harder nodes."""
    role = "attacker"

    def _variants(self, board):
        out, ids = [], set()
        for m in board.recall("attacks"):  # what worked before comes first
            v = m.get("variant") or {}
            if v.get("id") and v["id"] not in ids:
                out.append(v)
                ids.add(v["id"])
        out += [a for a in catalog.ATTACKS if a["id"] not in ids]
        return out

    def _trigger_worlds(self, board):
        ev = board.get("evals/v0.json") or {"results": {}}
        return [n["world"] for n in load_all(board, "nodes")
                if n["origin"] == "grid" and ev["results"].get(n["id"], {}).get("verdict") == "BLOCK"]

    def step(self, board):
        if not approved(board):
            return False
        import nodes
        did = False
        for name in open_tasks(board, "attack"):
            if not self.claim(board, name):
                continue
            did = True
            vid = name.split(".", 1)[1]
            target = board.get(f"versions/{vid}.json")
            worlds = sorted(self._trigger_worlds(board),
                            key=lambda w: [not w[k] for k in w])  # all_worlds order
            new = []
            for variant in self._variants(board):
                for world in worlds:
                    nid = nodes.node_id(world, variant["id"])
                    if board.exists(f"nodes/{nid}.json") or any(n["id"] == nid for n in new):
                        continue
                    node = {"id": nid, "world": world,
                            "variant": {k: variant[k] for k in ("id", "title", "injection")},
                            "origin": "attack", "added_by": self.name, "against": vid,
                            "backend": None, "sandbox_id": None, "fork_ms": 0.0,
                            "state": "pending"}
                    snap = nodes.materialize(node, "local")  # local check before a fork
                    if nodes.evaluate(target, node, snap)["verdict"] == "BLOCK":
                        new.append(node)
            for node in new:
                board.put(f"nodes/{node['id']}.json", node)
                post(board, f"node.{node['id']}")
                board.log(self.name, "attack", node=node["id"], against=vid,
                          variant=node["variant"]["id"])
            if new:
                count = len(load_all(board, "nodes"))
                for v in load_all(board, "versions"):
                    post(board, f"eval.{v['id']}@{count}", priority=0 if v["id"] == vid else 1)
            else:
                board.log(self.name, "attack", node=None, against=vid, result="no new bypass")
            self.finish(board, name)
        return did


CLASSES = {"scout": Scout, "lab": Lab, "fixer-code": FixerCode,
           "fixer-policy": FixerPolicy, "attacker": Attacker}


def make_role(role, n=1, backend="local"):
    return CLASSES[role](n=n, backend=backend)


# ------------------------------------------------------------------ scheduler

def run_scheduler(board, backend="local", seed=None, max_ticks=500, roles=None):
    """Every role in one process, one step each per tick (seeded order if seed is
    given, else the canonical order). Ends when 3 ticks in a row do no work:
    quiescent -> done, unapproved work left -> report-only."""
    roles = roles or [make_role(r, 1, backend) for r in ROLES]
    rng = random.Random(seed) if seed is not None else None
    idle = 0
    for _ in range(max_ticks):
        if board.exists("control/done.json"):
            return board.get("control/done.json")
        order = list(roles)
        if rng:
            rng.shuffle(order)
        did = False
        for r in order:
            did = r.step(board) or did
        idle = 0 if did else idle + 1
        if idle >= QUIET_POLLS:
            reason = ("quiescent" if quiescent(board)
                      else "report-only" if not approved(board) else "stalled")
            write_done(board, "swarm", reason)
    write_done(board, "swarm", "tick limit")
    return board.get("control/done.json")


# ------------------------------------------------------------- process mode

def load_dotenv():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


def board_from_args(kind, root=None):
    if kind == "local" and root:
        return open_board("local", root=root)
    return open_board(kind)


def run_process(role, board, poll=POLL_S):
    idle = 0
    try:
        while not board.exists("control/done.json"):
            try:
                did = role.step(board)
            except Exception as e:  # keep the peer alive; the board shows the error
                board.log(role.name, "error", msg=f"{type(e).__name__}: {e}")
                did = False
            idle = 0 if did or not quiescent(board) else idle + 1
            if idle >= QUIET_POLLS:
                write_done(board, role.name, "quiescent")
            time.sleep(poll)
    finally:
        if role.backend == "archil":
            try:
                import nodes
                nodes.cleanup_archil()
            except Exception:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("role", choices=ROLES)
    ap.add_argument("--board", default=os.environ.get("AIRLOCK_BOARD", "local"))
    ap.add_argument("--backend", default="local", choices=("local", "archil"))
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--root", default=None, help="LocalBoard directory")
    a = ap.parse_args(argv)
    load_dotenv()
    run_process(make_role(a.role, a.n, a.backend), board_from_args(a.board, a.root))


if __name__ == "__main__":
    main()
