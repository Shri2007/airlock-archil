"""AIRLOCK -- 64 possible worlds, 4 clones per round. Hunt down the exact
conditions where an AI tool betrays you.

    python3 run_airlock.py                 # local folders, free, instant
    USE_ARCHIL=1 python3 run_airlock.py    # real Archil microVM forks
    python3 run_airlock.py --grid          # fallback: brute-force all 64 locally
    python3 run_airlock.py --seed 3        # different random start

Writes results.json (the contract with viewer.html).
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "results.json"


def load_dotenv():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


load_dotenv()

from environments import KNOBS  # noqa: E402
from recorder import make_recorder  # noqa: E402
from search import PER_ROUND, Search, run_grid  # noqa: E402
from workspace import make_workspace, using_archil  # noqa: E402

DOT = {0: ".", 1: "1", 2: "2", 3: "3", 4: "4", 5: "X"}


def show_round(r):
    print(f"\nROUND {r['round']}  [{r['phase'].upper()}]")
    for e in r["envs"]:
        bar = "#" * e["score"] + "." * (5 - e["score"])
        tag = "RED  BETRAYS" if e["verdict"] == "BLOCK" else f"score {e['score']}/5 "
        how = f"flip {e['flipped']:<10}" if e["flipped"] else "random          "
        cached = " (cached)" if e["source"] == "cached" else ""
        print(f"  {e['id']:<6} {how} [{bar}] {tag}  {e['reason']}{cached}")
    sys.stdout.flush()


def write(payload):
    RESULTS.write_text(json.dumps(payload, indent=2))


def main():
    args = sys.argv[1:]
    seed = int(args[args.index("--seed") + 1]) if "--seed" in args else 7
    mode = "archil" if using_archil() else "local"

    rec = make_recorder()
    rec.clear()
    base = make_workspace(rec, run="base")
    t0 = time.time()
    print("=" * 70)
    print(f"AIRLOCK  |  backend: {base.kind}  |  64 possible worlds, {PER_ROUND} clones/round")
    print("=" * 70)
    try:
        base.seed()
        if "--grid" in args:
            if mode == "archil":
                raise SystemExit("--grid is local-only (64 forks would burn the free tier)")
            rows = run_grid(base)
            bad = [r for r in rows if r["verdict"] == "BLOCK"]
            print(f"Brute force: betrayed in {len(bad)} of {len(rows)} worlds")
            write({"mode": mode, "environments": rows, "betrayed": len(bad),
                   "total": len(rows), "trigger": []})
            return
        s = Search(base, seed=seed, on_round=show_round)
        best, trigger, irrelevant = s.run()
    finally:
        base.destroy()

    flat = [e for r in s.rounds for e in r["envs"]]
    secs = round(time.time() - t0, 1)
    write({
        "mode": mode, "seed": seed, "space": 2 ** len(KNOBS), "knobs": KNOBS,
        "rounds": s.rounds,
        "environments": flat,  # flat list, what the grid viewer reads
        "betrayed": sum(1 for e in flat if e["verdict"] == "BLOCK"),
        "total": len(flat),
        "unique_worlds_tested": len(s.seen),
        "sandboxes_forked": s.forks,
        "found_in_round": next((r["round"] for r in s.rounds
                                if any(e["score"] == 5 for e in r["envs"])), None),
        "trigger": trigger,
        "irrelevant": irrelevant,
        "seconds": secs,
    })

    print("\n" + "=" * 70)
    if trigger:
        print(f"Found the betrayal after testing {len(s.seen)} of 64 worlds "
              f"({s.forks} forks, {secs}s).")
        print("MINIMAL TRIGGER: betrays only when " + " + ".join(trigger))
        if irrelevant:
            print("Doesn't matter: " + ", ".join(irrelevant))
    else:
        print(f"No betrayal found in {len(s.rounds)} rounds (best score {best['score']}/5).")
    print("=" * 70)


if __name__ == "__main__":
    main()
