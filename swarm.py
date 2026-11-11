"""Airlock swarm CLI (CONTRACTS.md section 4).

    python3 swarm.py up [--board local|archil] [--backend local|archil] [--yes|--no]
                        [--deterministic] [--seed N] [--forget] [--root DIR]
    python3 swarm.py approve
    python3 swarm.py report
    python3 swarm.py repro <NID> [--backend local|archil]

up         reset the run, then run the five
           roles: --deterministic runs them in one process with a round-robin
           scheduler; otherwise one process per role, coordinated only through
           the board. --yes approves up front; --no is report only (v0 is scored,
           nothing is fixed); neither waits for `approve` or the viewer button.
           memory/ (accepted fixes, attacks that worked) survives runs: a second
           `up` starts from what the last swarm learned. --forget clears it first
           for the full story (use it for demos and reproducibility checks).
approve    write control/approved.json (by: cli)
report     out/airlock.patch (champion), out/airlock-policy.json (best policy
           version), out/board.json (snapshot), then report.py if present
repro      rebuild one node's sandbox (archil: kept PAUSED as a forkable repro)
           and show what v0 does there
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import peers
from board import champion as pick_champion

HERE = Path(__file__).parent
OUT = HERE / "out"


# ------------------------------------------------------------------ printing

def fmt(e):
    ev, who = e["event"], e.get("agent", "?")
    if ev == "version":
        src = f" from {e['parent']}" if e.get("parent") else ""
        msg = f"+ version {e['id']} [{e.get('track', '?')}]{src}: {e.get('title', '')}"
    elif ev == "node":
        msg = (f"{e['count']} worlds posted" if "count" in e else
               f"built {e['id']} ({e.get('backend', '?')}, {e.get('fork_ms', 0):.0f} ms"
               + (f", {e['sandbox_id']}" if e.get("sandbox_id") else "") + ")")
    elif ev == "claimed":
        msg = f"claimed {e['task']}"
    elif ev == "eval":
        msg = (f"{e['version']} scored {e['passes']}/{e['total']}"
               + ("" if e.get("utility", True) else "  (utility BROKEN)"))
    elif ev == "attack":
        msg = (f"attack {e.get('variant')} on {e['node']} beats {e['against']}" if e.get("node")
               else f"no new bypass against {e['against']}")
    elif ev == "champion":
        msg = f"*** champion {e['version']} ({e['passes']}/{e['total']})"
    elif ev == "approved":
        msg = "approved: fixers and attacker may start"
    elif ev == "done":
        msg = f"done ({e.get('reason')})"
    elif ev == "error":
        msg = f"! {e.get('msg')}"
    else:
        msg = json.dumps({k: v for k, v in e.items() if k not in ("seq", "ts", "agent", "event")})
        msg = f"{ev} {msg}"
    return f"{e.get('seq', 0):>4}  {who:<16} {msg}"


def tail(board, since, quiet_nodes=False):
    evs = board.read_log(since)
    for e in evs:
        if quiet_nodes and e["event"] == "node" and "count" not in e:
            continue
        print(fmt(e), flush=True)
    return since + len(evs)


# ------------------------------------------------------------------- helpers

def get_board(a):
    return peers.board_from_args(a.board, getattr(a, "root", None))


def approve(board, by="cli"):
    if not board.exists("control/approved.json"):
        board.put("control/approved.json", {"by": by, "ts": time.time()})
        board.log(by, "approved")


def forget(board):
    for kind in ("fixes", "attacks"):
        board.delete(f"memory/{kind}.jsonl")


def report_only_done(board):
    """--no: v0 is scored and only approval-gated work is left."""
    if not board.exists("evals/v0.json") or board.list("tasks/claimed") \
            or peers.pending_nodes(board):
        return False
    return all(peers.task_name(p) in ("fix-code.v0", "fix-policy.v0", "attack.v0")
               for p in board.list("tasks/open"))


# ------------------------------------------------------------------- commands

def cmd_up(a):
    peers.load_dotenv()
    board = get_board(a)
    if a.forget:
        forget(board)
    board.reset_run()
    if a.yes:
        approve(board)
    print(f"airlock swarm | board {a.board} | backend {a.backend} | "
          f"{'deterministic' if a.deterministic else 'one process per role'} | "
          f"{'approved' if a.yes else 'report only' if a.no else 'waiting for approval'} | "
          f"memory: {len(board.recall('fixes'))} fixes, {len(board.recall('attacks'))} attacks",
          flush=True)
    t0 = time.time()
    if a.deterministic:
        done = peers.run_scheduler(board, backend=a.backend, seed=a.seed)
        tail(board, 0, quiet_nodes=True)
    else:
        done = run_processes(board, a)
    snap = board.snapshot()
    ev = {e["version"]: e for e in snap["evals"]}
    print("-" * 70)
    for v in snap["versions"]:
        e = ev.get(v["id"])
        score = f"{e['passes']}/{e['total']}" if e else "not scored"
        crown = "  <- champion" if v["id"] == snap["champion"] else ""
        print(f"  {v['id']:<6} {v['track']:<6} {score:<10} {v['title']}{crown}")
    print(f"done: {done.get('reason') if done else '?'} in {time.time() - t0:.1f}s | "
          f"{len(snap['nodes'])} nodes | champion {snap['champion']} | "
          f"memory {snap['memory']}")
    return 0


def run_processes(board, a):
    base = [sys.executable, str(HERE / "peers.py")]
    extra = ["--board", a.board, "--backend", a.backend]
    if a.root:
        extra += ["--root", a.root]
    procs = [subprocess.Popen(base + [role] + extra + ["--n", "1"], cwd=HERE)
             for role in peers.ROLES]
    since, hinted, quiet = 0, False, 0
    try:
        while not board.exists("control/done.json"):
            since = tail(board, since)
            if a.no:
                quiet = quiet + 1 if report_only_done(board) else 0
                if quiet >= peers.QUIET_POLLS:
                    peers.write_done(board, "swarm", "report-only")
            elif not hinted and board.exists("evals/v0.json") and not peers.approved(board):
                print("      waiting for approval: python3 swarm.py approve "
                      "(or the viewer's Approve button)", flush=True)
                hinted = True
            if all(p.poll() is not None for p in procs):
                peers.write_done(board, "swarm", "all peers exited")
            time.sleep(peers.POLL_S)
    except KeyboardInterrupt:
        peers.write_done(board, "swarm", "interrupted")
    finally:
        deadline = time.time() + 30
        for p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.terminate()
        tail(board, since)
    return board.get("control/done.json")


def cmd_approve(a):
    board = get_board(a)
    approve(board)
    print("approved")
    return 0


def cmd_report(a):
    board = get_board(a)
    snap = board.snapshot()
    OUT.mkdir(exist_ok=True)
    (OUT / "board.json").write_text(json.dumps(snap, indent=2))
    versions = {v["id"]: v for v in snap["versions"]}
    evals = {e["version"]: e for e in snap["evals"]}
    count = len(snap["nodes"])
    champ = snap["champion"]
    patch = versions[champ]["patch"] if champ in versions else ""
    (OUT / "airlock.patch").write_text(patch)
    pols = [v for v in snap["versions"] if v["track"] == "policy"
            and v["id"] in evals and evals[v["id"]]["nodes_seen"] >= count]
    best = max(pols, key=lambda v: (evals[v["id"]]["passes"], -v["generation"]), default=None)
    policy = None
    if best:
        e = evals[best["id"]]
        policy = {"version": best["id"], "title": best["title"], "rationale": best["rationale"],
                  "policy": best["policy"], "passes": e["passes"], "total": e["total"]}
    (OUT / "airlock-policy.json").write_text(json.dumps(policy, indent=2))
    print(f"champion {champ}: "
          + (f"{evals[champ]['passes']}/{evals[champ]['total']}" if champ in evals else "-")
          + f" | patch {len(patch.splitlines())} lines -> out/airlock.patch")
    if policy:
        print(f"policy   {policy['version']}: {policy['passes']}/{policy['total']} "
              f"-> out/airlock-policy.json")
    print(f"board    {count} nodes, {len(versions)} versions -> out/board.json")
    rep = HERE / "report.py"
    if rep.exists():
        r = subprocess.run([sys.executable, str(rep), str(OUT / "board.json"),
                            str(OUT / "airlock-report.html")], cwd=HERE)
        if r.returncode == 0:
            print("report   -> out/airlock-report.html")
        else:
            print(f"report.py exited {r.returncode}")
    return 0


def cmd_repro(a):
    peers.load_dotenv()
    import catalog
    import nodes
    board = get_board(a)
    node = board.get(f"nodes/{a.nid}.json")
    if node is None:
        bits, _, variant = a.nid.removeprefix("n-").partition("~")
        if len(bits) != 6 or set(bits) - {"0", "1"}:
            print(f"unknown node {a.nid}")
            return 2
        from environments import KNOBS
        world = {k: b == "1" for k, b in zip(KNOBS, bits)}
        var = catalog.attack_by_id(variant) if variant else None
        node = {"id": a.nid, "world": world, "variant": var,
                "origin": "attack" if var else "grid"}
    v0 = board.get("versions/v0.json") or {"id": "v0", "files": {}, "policy": None}
    m = nodes.materialize(node, a.backend, keep=(a.backend == "archil"))
    r = nodes.evaluate(v0, node, {"files": m["files"], "network": m["network"]})
    print(f"{a.nid}  world {node['world']}")
    if node.get("variant"):
        print(f"  attack {node['variant']['id']}: {node['variant']['title']}")
    print(f"  v0 -> {r['verdict']} (score {r['score']}/5): {r['reason']}")
    for e in r["events"]:
        print(f"     {e['kind']:<15} {e['target']}" + (f"  leaked {e['leaked']}" if e["leaked"] else ""))
    if m.get("sandbox_id"):
        print(f"  sandbox {m['sandbox_id']} kept PAUSED (fork it to reproduce; delete when done)")
    if a.backend == "archil":
        nodes.cleanup_archil()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--board", default=os.environ.get("AIRLOCK_BOARD", "local"),
                       choices=("local", "archil"))
        p.add_argument("--root", default=None, help="LocalBoard directory (default .airlock-board)")

    up = sub.add_parser("up")
    common(up)
    up.add_argument("--backend", default="local", choices=("local", "archil"))
    g = up.add_mutually_exclusive_group()
    g.add_argument("--yes", action="store_true", help="approve fixes up front")
    g.add_argument("--no", action="store_true", help="report only: score v0, fix nothing")
    up.add_argument("--deterministic", action="store_true")
    up.add_argument("--seed", type=int, default=None)
    up.add_argument("--forget", action="store_true", help="clear memory/ first (fresh, reproducible story); "
                    "without it a run starts from what the last run learned")
    for name in ("approve", "report"):
        common(sub.add_parser(name))
    rp = sub.add_parser("repro")
    common(rp)
    rp.add_argument("nid")
    rp.add_argument("--backend", default="local", choices=("local", "archil"))
    a = ap.parse_args(argv)
    return {"up": cmd_up, "approve": cmd_approve, "report": cmd_report,
            "repro": cmd_repro}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
