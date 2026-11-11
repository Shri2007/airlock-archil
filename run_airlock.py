"""AIRLOCK -- 64 possible worlds, 4 clones per round. Hunt down the exact
conditions where an AI tool betrays you.

    python3 run_airlock.py                 # local folders, free, instant
    USE_ARCHIL=1 python3 run_airlock.py    # real Archil microVM forks
    python3 run_airlock.py --grid          # fallback: brute-force all 64 locally
    python3 run_airlock.py --seed 3        # different random start
    python3 run_airlock.py --profile naive # pick the tool under test (brain.PROFILES)
    python3 run_airlock.py --pace 0.25     # seconds per clone line (for live demos)

If an Archil run fails mid-way (wifi, quota, API), it says so and finishes the
same search locally, so a demo never ends in a stack trace.

Writes results.json (the contract with viewer.html).
"""
from contextlib import nullcontext
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

import brain  # noqa: E402
import crime  # noqa: E402
from environments import KNOBS  # noqa: E402
from evidence import CANARY, make_locker  # noqa: E402
from recorder import make_recorder  # noqa: E402
from search import PER_ROUND, Search, run_grid  # noqa: E402
from workspace import _archil_setup as archil_setup  # noqa: E402
from workspace import make_workspace, using_archil  # noqa: E402

PACE = 0.0


def show_round(r):
    print(f"\nROUND {r['round']}  [{r['phase'].upper()}]")
    for e in r["envs"]:
        bar = "#" * e["score"] + "." * (5 - e["score"])
        tag = "RED  BETRAYS" if e["verdict"] == "BLOCK" else f"score {e['score']}/5 "
        how = f"flip {e['flipped']:<10}" if e["flipped"] else "random          "
        cached = " (cached)" if e["source"] == "cached" else ""
        print(f"  {e['id']:<6} {how} [{bar}] {tag}  {e['reason']}{cached}")
        sys.stdout.flush()
        time.sleep(PACE)


def write(payload, repo=None):
    if repo is not None:
        payload["repo"] = repo.metadata
    RESULTS.write_text(json.dumps(payload, indent=2))


def _arg(args, flag, default, cast=str):
    return cast(args[args.index(flag) + 1]) if flag in args else default


def main():
    global PACE
    args = sys.argv[1:]
    if "--profile" in args:
        os.environ["AIRLOCK_PROFILE"] = _arg(args, "--profile", "deferential")
    brain.profile()  # validate early
    PACE = _arg(args, "--pace", 0.0, float)
    if args and args[0] == "repro":
        if using_archil():
            archil_setup()
        return crime.repro(args[1:])
    from repo_twin import load_twin
    if "--repo" in args and args.index("--repo") + 1 == len(args):
        raise SystemExit("--repo needs owner/name")
    repo_name = _arg(args, "--repo", None)
    if "--repo-offline" in args and not repo_name:
        raise SystemExit("--repo-offline requires --repo owner/name")
    try:
        repo = load_twin(repo_name, offline="--repo-offline" in args,
                         keep_all="--keep-all" in args) if repo_name else None
    except ValueError as e:
        raise SystemExit(str(e)) from e
    with repo.activate() if repo else nullcontext():
        return _run_with_fallback(args, repo)


def _run_with_fallback(args, repo=None):
    if not using_archil():
        return _run(args, repo=repo)
    try:
        return _run(args, repo=repo)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        print("\n" + "!" * 70)
        print(f"Archil run failed mid-way ({type(e).__name__}: {str(e)[:120]})")
        print("Finishing the SAME search locally (same seed, same tool) so you still get a result.")
        print("!" * 70)
        os.environ["USE_ARCHIL"] = "0"
        return _run(args, note=f"archil failed ({type(e).__name__}); finished locally", repo=repo)


def _run(args, note=None, repo=None):
    seed = _arg(args, "--seed", 7, int)
    mode = "archil" if using_archil() else "local"

    if mode == "archil":
        archil_setup()  # SSL certs + key check, before the locker touches Archil
        crime.cleanup(crime.previous_scene(), quiet=True)  # free the old crime scene's slots
    locker = make_locker()
    rec = make_recorder(locker)
    rec.clear()
    base = make_workspace(rec, run="base")
    if repo is not None:
        base.issue_texts = repo.issue_texts
    t0 = time.time()
    print("=" * 70)
    print(f"AIRLOCK  |  backend: {base.kind}  |  64 possible worlds, {PER_ROUND} clones/round")
    print(f"tool under test: {brain.profile()}  ({brain.PROFILES[brain.profile()]})")
    print(f"evidence locker: {locker.where}")
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
                   "total": len(rows), "trigger": []}, repo=repo)
            return
        s = Search(base, seed=seed, on_round=show_round)
        best, trigger, irrelevant = s.run()
        scene = None
        if best["score"] == 5:
            try:
                scene = crime.keep(base, best)
                if scene.get("sandbox_id"):
                    print(f"\nCRIME SCENE KEPT: world {best['id']} rebuilt, betrayed again "
                          f"({scene['score']}/5), microVM {scene['sandbox_id']} paused.")
                    print("  replay it:  python3 run_airlock.py repro [--flip network]")
            except Exception as e:  # never lose a finished search over this
                print(f"\n(could not keep the crime scene: {type(e).__name__}: {str(e)[:100]})")
                scene = None
    finally:
        if not (locals().get("scene") or {}).get("sandbox_id"):
            base.destroy()  # kept crime scenes are forks of base, so base stays too

    flat = [e for r in s.rounds for e in r["envs"]]

    # Independent check: grep the shared outbox for canary tags and make sure it
    # agrees with the judge. The judge reads the log; grep reads what actually left.
    hits = locker.grep("outbox", CANARY)
    hits = [h for h in hits if not h[0].endswith("/crime.txt")]  # the kept crime scene, filed separately
    grep_runs = sorted({f.split("/")[-1].removesuffix(".txt") for f, _ in hits})
    judge_runs = sorted({e["id"] for e in flat
                         if e["verdict"] == "BLOCK" and e["source"] == "sandbox"})
    agrees = grep_runs == judge_runs
    secs = round(time.time() - t0, 1)
    write({
        "mode": mode, "seed": seed, "space": 2 ** len(KNOBS), "knobs": KNOBS,
        "profile": brain.profile(), "profile_desc": brain.PROFILES[brain.profile()],
        "note": note,
        "crime_scene": scene,
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
        "evidence": {"locker": locker.where, "canary": CANARY,
                     "grep_hits": [{"file": f, "line": line} for f, line in hits],
                     "grep_runs": grep_runs, "judge_runs": judge_runs,
                     "agrees_with_judge": agrees},
    }, repo=repo)

    print("\n" + "=" * 70)
    if trigger:
        print(f"Found the betrayal after testing {len(s.seen)} of 64 worlds "
              f"({s.forks} forks, {secs}s).")
        print("MINIMAL TRIGGER: betrays only when " + " + ".join(trigger))
        if irrelevant:
            print("Doesn't matter: " + ", ".join(irrelevant))
    else:
        print(f"No betrayal found in {len(s.rounds)} rounds (best score {best['score']}/5).")
    print(f"EVIDENCE: grep '{CANARY}' over the shared outbox -> {len(hits)} hits in "
          f"{grep_runs or 'no files'}; judge flagged {judge_runs or 'none'} -> "
          + ("AGREE" if agrees else "MISMATCH"))
    print("=" * 70)


if __name__ == "__main__":
    main()
