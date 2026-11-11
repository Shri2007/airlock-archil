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
import os
import re
import time
from pathlib import Path

from environments import KNOBS, Env, apply
from judge import judge
from recorder import Recorder
from tool_under_test import run_tool
from workspace import ArchilWorkspace, Workspace, using_archil

RESULTS = Path(__file__).parent / "results.json"
# The paused sandboxes' ids live in their OWN file. results.json is rewritten by
# every run (including local ones, which carry no sandbox id), so keeping the ids
# only there made `repro --cleanup` silently skip live sandboxes and leak slots.
# Lives under evidence/ because that folder is already gitignored.
CRIME_FILE = Path(__file__).parent / "evidence" / "crime_scene.json"
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
        CRIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        CRIME_FILE.write_text(json.dumps(rec, indent=2))
    else:
        crime.destroy()
    return rec


def _delete(sb):
    try:
        sb.delete()
    except Exception:
        sb.stop()
        sb.delete()


def sweep(quiet=False):
    """Delete orphaned crime-scene sandboxes this tool made: PAUSED sandboxes named
    exactly `airlock-crime` or `airlock-base-<digits>`. Paused-only on purpose: a
    base that is running belongs to a search in progress. Never raises."""
    if not using_archil():
        return
    try:
        import archil
        found = archil.list_sandboxes()
        items = list(getattr(found, "items", found))
        orphans = [s for s in items if str(s.status).endswith("paused")
                   and (s.name == "airlock-crime" or re.fullmatch(r"airlock-base-\d+", s.name or ""))]
        for s in sorted(orphans, key=lambda s: s.name != "airlock-crime"):  # fork before base
            try:
                _delete(s)
                if not quiet:
                    print(f"  swept orphan {s.name}")
            except Exception as e:
                if not quiet:
                    print(f"  could not delete {s.name}: {type(e).__name__}")
    except Exception as e:
        if not quiet:
            print(f"  (orphan sweep skipped: {type(e).__name__})")


def cleanup(scene, quiet=False):
    """Delete the kept crime scene (fork first, then its base), then sweep orphans."""
    try:
        CRIME_FILE.unlink()
    except FileNotFoundError:
        pass
    if scene and scene.get("sandbox_id") and using_archil():
        _cleanup_ids(scene, quiet)
    sweep(quiet)


def _cleanup_ids(scene, quiet=False):
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
    """The kept crime scene: the dedicated file wins (it survives later local runs),
    else whatever the last results.json recorded."""
    for path, pick in ((CRIME_FILE, lambda d: d), (RESULTS, lambda d: d.get("crime_scene"))):
        try:
            scene = pick(json.loads(path.read_text()))
            if scene:
                return scene
        except Exception:
            continue
    return None


def repro(args):
    scene = previous_scene()
    if "--cleanup" in args:  # works even with no record: it also sweeps orphans
        # Explicit intent to delete: do not require USE_ARCHIL=1. Without this the
        # command silently skipped live sandboxes and leaked free-tier slots.
        if os.environ.get("ARCHIL_API_KEY"):
            os.environ["USE_ARCHIL"] = "1"
            from workspace import _archil_setup
            _archil_setup()
        cleanup(scene)
        return
    if not scene:
        raise SystemExit("No crime scene recorded. Run a search that finds a betrayal first.")
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
