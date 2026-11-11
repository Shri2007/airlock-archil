"""Stage demo. Every scenario is a REAL search (no canned output): the tool under
test is a deterministic offline profile, so the same command gives the same
result every time, with no wifi, keys or quota needed. Proven by
tests/test_search.py: every profile, 25 seeds, matches brute force over all 64.

    python3 demo.py            # list scenarios
    python3 demo.py 1          # run one (paced for the audience)
    python3 demo.py all        # 1-4 back to back
    python3 demo.py repo       # real Flask README/issues, committed offline fixture
    python3 demo.py live       # scenario 1 on real Archil microVMs
                               # (falls back to local by itself if Archil fails)
    python3 demo.py replay              # fork the kept crime scene, betray again
    python3 demo.py whatif network      # fork it, cut the network, rerun
    python3 demo.py whatif authority    # ...or any other condition(s)

Each run writes results.json (what viewer.html shows) and a copy in
demo_results/<n>.json so you can flip between them.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

SCENARIOS = {
    "1": ("The trap", "deferential", 7,
          "A tool that only obeys its owner. Sounds safe. Airlock finds the 5 "
          "conditions where a fake 'owner' message makes it leak your keys."),
    "2": ("Same search, different tool", "naive", 7,
          "A tool that obeys anything. Different weakness, so Airlock finds a "
          "different trigger, and proves 'authority' doesn't matter here."),
    "3": ("What the real model did", "skeptic", 7,
          "Modeled on real Haiku 5.5 in our runs: it IGNORES fake-owner messages "
          "but obeys plain ones. The opposite of what we guessed."),
    "4": ("A safe tool", "hardened", 7,
          "A tool that treats issue text as data. Airlock searches 38 of 64 worlds "
          "and finds nothing. All green is a result too."),
    "repo": ("Real repository, sample tool", "deferential", 7,
             "Flask's real issues surround our planted attack. "
             "The summarizer is Airlock's sample tool."),
}


def banner(n, title, desc):
    print("\n" + "#" * 70)
    print(f"#  SCENARIO {n}: {title}")
    print("#  " + desc)
    print("#" * 70 + "\n")


def run(n, live=False):
    title, profile, seed, desc = SCENARIOS[n]
    banner(n if not live else f"{n} (LIVE on Archil)", title, desc)
    env = dict(os.environ, AIRLOCK_PROFILE=profile, USE_ARCHIL="1" if live else "0")
    pace = "0" if live else "0.25"  # real forks already take seconds each
    extra = ["--repo", "pallets/flask", "--repo-offline"] if n == "repo" else []
    if n == "repo":
        env["AGENT_BRAIN"] = "offline"
    code = subprocess.call([sys.executable, str(HERE / "run_airlock.py"),
                            "--seed", str(seed), "--pace", pace, *extra], env=env, cwd=HERE)
    out = HERE / "demo_results"
    out.mkdir(exist_ok=True)
    if (HERE / "results.json").exists():
        shutil.copy(HERE / "results.json", out / f"{n}{'-live' if live else ''}.json")
    return code


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in SCENARIOS:
        sys.exit(run(arg))
    if arg == "all":
        sys.exit(max(run(n) for n in ("1", "2", "3", "4")))
    if arg == "live":
        sys.exit(run("1", live=True))
    if arg in ("replay", "whatif"):
        # Fork the kept crime scene (paused microVM on Archil after `demo.py live`;
        # rebuilt locally otherwise) and rerun, optionally flipping conditions.
        flips = [x for k in sys.argv[2:] for x in ("--flip", k)]
        sys.exit(subprocess.call([sys.executable, str(HERE / "run_airlock.py"), "repro", *flips],
                                 cwd=HERE))
    print(__doc__)
    for n, (title, profile, _, desc) in SCENARIOS.items():
        print(f"  {n}  {title:<28} tool profile: {profile}")
        print(f"     {desc}\n")


if __name__ == "__main__":
    main()
