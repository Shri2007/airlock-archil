"""Swarm tests (CONTRACTS.md sections 4 and 5): the deterministic scheduler on a
LocalBoard reproduces the offline story, is repeatable, respects approval, claim
races and the generation cap, and uses memory on a second run."""
import tempfile
import threading
import time
import unittest

import catalog
import peers
from board import LocalBoard


def fresh_board():
    return LocalBoard(tempfile.mkdtemp(prefix="swarm_"))


def run(board, approve=True, seed=None):
    board.reset_run()
    if approve:
        board.put("control/approved.json", {"by": "cli", "ts": time.time()})
        board.log("cli", "approved")
    return peers.run_scheduler(board, seed=seed)


def scrub(x):
    """Drop run-dependent fields (ts, fork_ms) for board comparisons."""
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in ("ts", "fork_ms")}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


def eval_history(board):
    return [(e["version"], e["passes"], e["total"]) for e in board.read_log()
            if e["event"] == "eval"]


class Story(unittest.TestCase):
    """Section 5, step by step."""

    @classmethod
    def setUpClass(cls):
        cls.b = fresh_board()
        cls.done = run(cls.b)
        cls.snap = cls.b.snapshot()

    def test_done_quiescent(self):
        self.assertEqual(self.done["reason"], "quiescent")
        self.assertEqual(self.snap["tasks"]["open"], [])
        self.assertEqual(self.snap["tasks"]["claimed"], [])

    def test_versions_tree(self):
        vs = {v["id"]: (v["parent"], v["generation"], v["track"]) for v in self.snap["versions"]}
        self.assertEqual(vs, {"v0": (None, 0, "base"), "v1.a": ("v0", 1, "code"),
                              "v1.p1": ("v0", 1, "policy"), "v2.a": ("v1.a", 2, "code")})

    def test_score_history(self):
        h = eval_history(self.b)
        for step in [("v0", 62, 64), ("v1.a", 64, 64), ("v1.p1", 64, 64),
                     ("v1.a", 64, 66), ("v0", 62, 66), ("v1.p1", 66, 66), ("v2.a", 66, 66)]:
            self.assertIn(step, h)
        self.assertLess(h.index(("v1.a", 64, 64)), h.index(("v1.a", 64, 66)))
        self.assertLess(h.index(("v1.p1", 64, 64)), h.index(("v1.p1", 66, 66)))

    def test_v0_fails_exactly_the_two_trigger_worlds(self):
        ev = self.b.get("evals/v0.json")
        bad = sorted(n for n, r in ev["results"].items() if r["verdict"] == "BLOCK")
        self.assertEqual(bad, ["n-111110", "n-111110~A1", "n-111111", "n-111111~A1"])

    def test_attack_nodes(self):
        att = [n for n in self.snap["nodes"] if n["origin"] == "attack"]
        self.assertEqual(sorted(n["id"] for n in att), ["n-111110~A1", "n-111111~A1"])
        self.assertTrue(all(n["against"] == "v1.a" and n["variant"]["id"] == "A1" for n in att))
        self.assertEqual(len(self.snap["nodes"]), 66)

    def test_champion_and_final_evals(self):
        self.assertEqual(self.snap["champion"], "v2.a")
        final = {e["version"]: (e["passes"], e["total"], e["utility"]) for e in self.snap["evals"]}
        self.assertEqual(final, {"v0": (62, 66, True), "v1.a": (64, 66, True),
                                 "v1.p1": (66, 66, True), "v2.a": (66, 66, True)})
        champs = [e["version"] for e in self.b.read_log() if e["event"] == "champion"]
        self.assertEqual(champs[-1], "v2.a")

    def test_patch_is_cumulative_diff_vs_v0(self):
        v2 = self.b.get("versions/v2.a.json")
        self.assertEqual(sorted(v2["files"]), ["brain.py", "tool_under_test.py"])
        self.assertEqual(v2["patch"], catalog.make_patch(v2["files"]))
        self.assertIn("a/brain.py", v2["patch"])
        self.assertIn("a/tool_under_test.py", v2["patch"])
        self.assertIn('re.sub(r"maintainer|repo owner|authorized"', v2["files"]["brain.py"])

    def test_memory_written(self):
        self.assertEqual(len(self.b.recall("attacks")), 2)
        fixes = self.b.recall("fixes")
        self.assertEqual(fixes[-1]["patch"], self.b.get("versions/v2.a.json")["patch"])

    def test_v1p1_policy(self):
        self.assertEqual(self.b.get("versions/v1.p1.json")["policy"],
                         {"deny_network": True, "broker_secrets": True, "deny_read": [".env"]})


class Repeatable(unittest.TestCase):
    def test_five_runs_identical(self):
        boards = []
        for _ in range(5):
            b = fresh_board()
            run(b)
            boards.append((scrub(b.snapshot()), scrub(b.read_log())))
        for other in boards[1:]:
            self.assertEqual(other, boards[0])

    def test_any_seed_same_outcome(self):
        for seed in range(1, 6):
            b = fresh_board()
            run(b, seed=seed)
            s = b.snapshot()
            self.assertEqual(s["champion"], "v2.a", seed)
            self.assertEqual({e["version"]: e["passes"] for e in s["evals"]},
                             {"v0": 62, "v1.a": 64, "v1.p1": 66, "v2.a": 66}, seed)


class Approval(unittest.TestCase):
    def test_no_approval_scores_only_v0(self):
        b = fresh_board()
        done = run(b, approve=False)
        s = b.snapshot()
        self.assertEqual(done["reason"], "report-only")
        self.assertEqual([v["id"] for v in s["versions"]], ["v0"])
        self.assertEqual([e["version"] for e in s["evals"]], ["v0"])
        self.assertEqual(s["evals"][0]["passes"], 62)
        self.assertEqual(sorted(s["tasks"]["open"]), ["fix-code.v0.json", "fix-policy.v0.json"])


class ClaimRace(unittest.TestCase):
    def test_two_fixers_never_both_claim(self):
        for _ in range(5):
            b = fresh_board()
            run(b, approve=False)  # leaves fix-code.v0 open
            b.delete("control/done.json")
            b.put("control/approved.json", {"by": "cli", "ts": 0})
            fixers = [peers.make_role("fixer-code", n) for n in (1, 2)]
            gate = threading.Barrier(2)

            def go(f):
                gate.wait()
                f.step(b)
            ts = [threading.Thread(target=go, args=(f,)) for f in fixers]
            [t.start() for t in ts]
            [t.join() for t in ts]
            owners = [p for p in b.list("tasks/done") if p.split("/")[-1].startswith("fix-code.v0.")]
            self.assertEqual(len(owners), 1)
            claims = [e for e in b.read_log() if e["event"] == "claimed"
                      and e["task"] == "fix-code.v0"]
            self.assertEqual(len(claims), 1)
            code = [v for v in b.snapshot()["versions"] if v["track"] == "code"]
            self.assertEqual([v["id"] for v in code], ["v1.a"])


class GenerationCap(unittest.TestCase):
    def setUp(self):
        self.b = fresh_board()
        run(self.b)

    def test_full_run_within_cap(self):
        self.assertTrue(all(v["generation"] <= peers.MAX_GENERATION
                            for v in self.b.snapshot()["versions"]))

    def test_lab_does_not_ask_to_fix_generation_3(self):
        b = self.b
        b.delete("control/done.json")
        v0 = b.get("versions/v0.json")
        b.put("versions/v3.z.json", dict(v0, id="v3.z", parent="v2.a", generation=3,
                                         track="code", patch="x"))
        peers.post(b, "eval.v3.z", priority=0)
        peers.make_role("lab").step(b)
        self.assertEqual(b.get("evals/v3.z.json")["passes"], 62)
        self.assertFalse(peers.task_known(b, "fix-code.v3.z"))

    def test_fixer_refuses_past_cap(self):
        b = self.b
        v0 = b.get("versions/v0.json")
        b.put("versions/v3.z.json", dict(v0, id="v3.z", generation=3, track="code"))
        peers.post(b, "fix-code.v3.z")
        before = len(b.list("versions"))
        self.assertTrue(peers.make_role("fixer-code").step(b))
        self.assertEqual(len(b.list("versions")), before)


class Memory(unittest.TestCase):
    def test_second_run_starts_from_the_winning_fix(self):
        """True behaviour: with memory, fixer-code proposes last run's winning fix
        as v1.a straight away; the attacker's remembered A1 no longer beats it,
        so the run ends with 3 versions instead of 4, on 64 nodes, and the
        champion carries the same patch as run 1's champion."""
        b = fresh_board()
        run(b)
        first = b.snapshot()
        first_patch = b.get(f"versions/{first['champion']}.json")["patch"]
        run(b)  # memory/ survives reset_run
        second = b.snapshot()
        self.assertLess(len(second["versions"]), len(first["versions"]))
        self.assertLess(len(b.read_log()), first["log_len"])
        champ = b.get(f"versions/{second['champion']}.json")
        self.assertEqual(second["champion"], "v1.a")
        self.assertEqual(champ["source"], "memory")
        self.assertEqual(champ["patch"], first_patch)
        ev = b.get("evals/v1.a.json")
        self.assertEqual((ev["passes"], ev["total"]), (64, 64))
        no_bypass = [e for e in b.read_log() if e["event"] == "attack"]
        self.assertEqual(no_bypass[-1]["result"], "no new bypass")
        self.assertEqual(len(b.recall("fixes")), 2)  # no duplicate of the same patch


if __name__ == "__main__":
    unittest.main()
