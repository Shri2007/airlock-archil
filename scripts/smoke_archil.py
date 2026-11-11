"""Live Archil smoke check for the infra layer. Costs a few cents; cleans up.

  1. ArchilBoard on disk `airlock-board`: put/get/list/log/claim round-trip,
     plus a 2-thread claim race with exactly one winner.
  2. materialize 2 nodes on real forks (n-111110, n-000000) and evaluate v0:
     n-111110 must BLOCK, n-000000 must PASS.
  3. Delete every sandbox this script created; verify with list_sandboxes().

Run: /Users/vishnusrinivas/airlock-wt/.venv/bin/python scripts/smoke_archil.py
At most 3 sandboxes alive (base + up to 2 forks; forks run one at a time here).
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
from workspace import _archil_setup  # noqa: E402

_archil_setup()
import archil  # noqa: E402

import nodes  # noqa: E402
from board_archil import ArchilBoard  # noqa: E402

V0 = {"id": "v0", "track": "base", "files": {}, "policy": None}
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def live_ids():
    return {s.id: s for s in archil.list_sandboxes()
            if str(getattr(s, "status", "")).lower().split(".")[-1] not in ("deleted",)}


def board_checks():
    print("1. ArchilBoard on disk airlock-board")
    t0 = time.perf_counter()
    b = ArchilBoard("airlock-board")
    ns = f"smoke/{int(time.time())}"
    b.put(f"{ns}/a.json", {"a": 1})
    check("put/get", b.get(f"{ns}/a.json") == {"a": 1})
    check("exists/missing", b.exists(f"{ns}/a.json") and b.get(f"{ns}/nope.json") is None)
    check("list", b.list(ns) == [f"{ns}/a.json"], str(b.list(ns)))
    n0 = len(b.read_log())
    b.log("smoke#1", "smoke", ns=ns)
    tail = b.read_log(since=n0)
    check("log append + seq", len(tail) == 1 and tail[0]["seq"] == n0 and tail[0]["ns"] == ns)
    b.put(f"{ns}/open/t.json", {"task": 1})
    wins = []

    def grab(i):
        if b.claim(f"{ns}/open/t.json", f"{ns}/claimed/t.smoke-{i}.json"):
            wins.append(i)

    ts = [threading.Thread(target=grab, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("claim race: exactly one winner", len(wins) == 1, f"winners={wins}")
    if wins:
        check("claimed object moved", b.list(f"{ns}/open") == [] and
              b.get(f"{ns}/claimed/t.smoke-{wins[0]}.json") == {"task": 1})
    for k in b.list(ns):
        b.delete(k)
    check("cleanup smoke keys", b.list(ns) == [])
    print(f"   board checks took {time.perf_counter() - t0:.1f}s")


def node_checks():
    print("2. materialize on real forks + evaluate v0")
    expect = {"n-111110": "BLOCK", "n-000000": "PASS"}
    t0 = time.perf_counter()
    nodes.archil_base()
    print(f"   base seeded in {time.perf_counter() - t0:.1f}s")
    worlds = {nodes.node_id(w): w for w in nodes.all_worlds()}
    for nid, want in expect.items():
        node = {"id": nid, "world": worlds[nid], "variant": None}
        snap = nodes.materialize(node, backend="archil")
        local = nodes.materialize(node, backend="local")
        check(f"{nid} snapshot matches local", snap["files"] == local["files"]
              and snap["network"] == local["network"], f"fork_ms={snap['fork_ms']}")
        r = nodes.evaluate(V0, node, snap)
        check(f"{nid} v0 {want}", r["verdict"] == want, f"score {r['score']}: {r['reason']}")


def main():
    try:
        board_checks()
        node_checks()
    finally:
        nodes.cleanup_archil()
        print("3. cleanup (only sandboxes this script created)")
        mine = set(nodes.created_sandboxes())
        alive = {}
        for _ in range(10):
            alive = {i: s for i, s in live_ids().items() if i in mine}
            if not alive:
                break
            for s in alive.values():  # retry stop+delete for anything of ours
                try:
                    s.stop()
                    s.delete()
                except Exception:
                    pass
            time.sleep(2)
        check("no sandboxes left behind", not alive,
              f"created={len(mine)}, still alive={sorted(alive)}")
    print(f"\nSMOKE {'OK' if all(results) else 'FAILED'}: {sum(results)}/{len(results)} checks")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
