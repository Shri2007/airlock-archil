"""The crime scene. After a search finds a betrayal, Airlock rebuilds that exact
world, lets the tool betray you in it once more, then PAUSES the microVM and
keeps it. Later you can fork the paused crime scene (~1.6s on Archil), change
one condition, and rerun:

    python3 run_airlock.py repro                   # replay the betrayal as-is
    python3 run_airlock.py repro --flip network    # "what if we'd cut the network?"
    python3 run_airlock.py repro --flip authority --flip secret
    python3 run_airlock.py repro --cleanup         # delete the kept machines

Locally there is no machine to keep, so repro rebuilds the world from its knobs.
Free-tier sandboxes live at most ~30 min, so keep the crime scene for the demo
by running the search shortly before you present.
"""
import json
import time
from pathlib import Path

from environments import KNOBS, Env, apply
from judge import judge
from recorder import Recorder
from tool_under_test import run_tool
from workspace import ArchilWorkspace, Workspace, using_archil

RESULTS = Path(__file__).parent / "results.json"
BAR = lambda s: "#" * s + "." * (5 - s)  # noqa: E731


def keep(base, best):
    """Rebuild the betraying world on a fork of base, betray once, pause it.
    Returns the crime_scene record for results.json."""
    env = Env(**{k: best[k] for k in KNOBS})
    crime = base.branch(run="crime")
    apply(crime, env)
    crime.ready()
    run_tool(crime)
    v = judge(crime.recorder, crime.run)
    rec = {"clone": best["id"], "knobs": env.as_dict(), "score": v["score"],
           "reason": v["reason"], "mode": "archil" if using_archil() else "local"}
    if isinstance(crime, ArchilWorkspace):
        crime.sb.pause()
        base.sb.pause()  # the crime scene is base's fork, so base must stay too
        rec.update(sandbox_id=crime.sb.id, base_id=base.sb.id, kept_at=time.time())
    else:
        crime.destroy()
    return rec


def cleanup(scene, quiet=False):
    """Delete a kept crime scene (fork first, then its base)."""
    if not scene or not scene.get("sandbox_id"):
        return
    import archil
    for key in ("sandbox_id", "base_id"):
        try:
            sb = archil.get_sandbox(scene[key])
            try:
                sb.delete()
            except Exception:
                sb.stop()
                sb.delete()
            if not quiet:
                print(f"  deleted {key} {scene[key]}")
        except Exception as e:
            if not quiet:
                print(f"  {key} {scene[key]} already gone ({type(e).__name__})")


def previous_scene():
    try:
        return json.loads(RESULTS.read_text()).get("crime_scene")
    except Exception:
        return None


def repro(args):
    scene = previous_scene()
    if not scene:
        raise SystemExit("No crime scene in results.json. Run a search that finds a betrayal first.")
    if "--cleanup" in args:
        cleanup(scene)
        return
    flips = [args[i + 1] for i, a in enumerate(args) if a == "--flip"]
    for k in flips:
        if k not in KNOBS:
            raise SystemExit(f"unknown knob {k!r}; pick from {KNOBS}")
    env = Env(**scene["knobs"])
    for k in flips:
        env = env.flip(k)

    print("=" * 70)
    print(f"CRIME SCENE  |  world {scene['clone']}  |  originally: {scene['reason']}")
    print("what-if:     " + (", ".join(f"{k} -> {'ON' if getattr(env, k) else 'OFF'}"
                                     for k in flips) or "replay unchanged"))
    print("=" * 70)

    rec = Recorder(path=Path(__file__).parent / "audit.jsonl")
    t0 = time.time()
    if scene.get("sandbox_id") and using_archil():
        import archil
        paused = ArchilWorkspace(rec, "crime", sandbox=archil.get_sandbox(scene["sandbox_id"]))
        replay = paused.branch(run="replay")
        print(f"forked the paused crime-scene microVM {scene['sandbox_id']} "
              f"in {time.time() - t0:.1f}s")
    else:
        base = Workspace(rec, run="base")
        base.seed()
        replay = base.branch(run="replay")
        base.destroy()
        print("local mode: rebuilt the crime scene from its knobs")
    try:
        apply(replay, env)
        replay.ready()
        run_tool(replay)
        v = judge(rec, "replay")
    finally:
        replay.destroy()

    tag = "RED  BETRAYS" if v["verdict"] == "BLOCK" else f"score {v['score']}/5"
    print(f"\n  replay  [{BAR(v['score'])}] {tag}  {v['reason']}")
    if flips:
        moved = scene["score"] - v["score"]
        print(f"\n  {'+'.join(flips)}: {scene['score']}/5 -> {v['score']}/5  "
              + ("(the attack still works)" if moved == 0 else
                 f"(the attack now stops at {v['score']}/5)"))
    print("=" * 70)
    return v
