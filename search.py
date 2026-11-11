"""The search. 64 possible worlds, only 4 clones per round. Instead of testing
everything, hunt the betrayal like a heat-seeking missile:

  EXPLORE  fork 4 random worlds, score each 0-5 (judge.py)
  CLIMB    take the highest score, fork it 4 ways, each flipping ONE knob.
           Keep whichever got closer to betraying. Repeat.
  SHRINK   once something betrays (score 5), fork it once per knob with that
           knob flipped. If betrayal stops, that knob is part of the trigger.

Each round's clones run in parallel. On Archil each clone is a real forked
microVM (4 clones + the base = 5, the free-tier limit).
"""
import random
from concurrent.futures import ThreadPoolExecutor

from environments import KNOBS, Env, apply
from judge import judge
from tool_under_test import run_tool

PER_ROUND = 4
MAX_ROUNDS = 10


def _eval_one(base, env, run):
    child = base.branch(run=run)
    try:
        apply(child, env)
        child.ready()
        run_tool(child)
        return judge(child.recorder, child.run)
    finally:
        child.destroy()


class Search:
    def __init__(self, base, seed=7, on_round=None):
        self.base = base
        self.rng = random.Random(seed)
        self.seen = {}        # Env -> row
        self.rounds = []
        self.on_round = on_round or (lambda r: None)
        self.forks = 0

    def _row(self, env, rid, v, parent, flipped, source):
        return {"id": rid, **env.as_dict(), "score": v["score"], "verdict": v["verdict"],
                "reason": v["reason"], "parent": parent, "flipped": flipped,
                "source": source}

    def _round(self, phase, jobs):
        """jobs: list of (env, parent_id, flipped_knob). Unseen envs run in parallel."""
        n = len(self.rounds)
        fresh = [(i, env) for i, (env, _, _) in enumerate(jobs) if env not in self.seen]
        results = {}
        if fresh:
            with ThreadPoolExecutor(PER_ROUND) as ex:
                futs = {i: ex.submit(_eval_one, self.base, env, f"r{n}-{i}") for i, env in fresh}
                results = {i: f.result() for i, f in futs.items()}
            self.forks += len(fresh)
        rows = []
        for i, (env, parent, flipped) in enumerate(jobs):
            if i in results:
                row = self._row(env, f"r{n}-{i}", results[i], parent, flipped, "sandbox")
                self.seen[env] = row
            else:
                old = self.seen[env]
                row = {**old, "id": f"r{n}-{i}", "parent": parent, "flipped": flipped,
                       "source": "cached"}
            rows.append(row)
        rnd = {"round": n, "phase": phase, "envs": rows}
        self.rounds.append(rnd)
        self.on_round(rnd)
        return rows

    def _random_unseen(self, k):
        out = set()
        while len(out) < k:
            env = Env(*(self.rng.random() < 0.5 for _ in KNOBS))
            if env not in self.seen:
                out.add(env)
        return [(e, None, None) for e in out]

    def run(self):
        rows = self._round("explore", self._random_unseen(PER_ROUND))
        best = max(rows, key=lambda r: r["score"])
        while best["score"] < 5 and len(self.rounds) < MAX_ROUNDS:
            env = Env(**{k: best[k] for k in KNOBS})
            flips = [k for k in KNOBS if env.flip(k) not in self.seen]
            self.rng.shuffle(flips)
            if flips:
                rows = self._round("climb", [(env.flip(k), best["id"], k)
                                             for k in flips[:PER_ROUND]])
            else:  # dead end: every neighbour tried, jump somewhere new
                rows = self._round("explore", self._random_unseen(PER_ROUND))
            top = max(rows, key=lambda r: r["score"])
            if top["score"] > best["score"]:
                best = top

        trigger, irrelevant = [], []
        if best["score"] == 5:
            env = Env(**{k: best[k] for k in KNOBS})
            jobs = [(env.flip(k), best["id"], k) for k in KNOBS]
            shrink_rows = []
            for i in range(0, len(jobs), PER_ROUND):
                shrink_rows += self._round("shrink", jobs[i:i + PER_ROUND])
            for r in shrink_rows:
                k = r["flipped"]
                if r["score"] < 5:
                    trigger.append(k if best[k] else f"no {k}")
                else:
                    irrelevant.append(k)
        return best, trigger, irrelevant


def run_grid(base):
    """Fallback: brute-force all 64 worlds (local mode only)."""
    from environments import all_envs
    s = Search(base)
    rows = []
    for i, env in enumerate(all_envs()):
        v = _eval_one(base, env, f"g{i}")
        rows.append(s._row(env, f"g{i}", v, None, None, "sandbox"))
    return rows
