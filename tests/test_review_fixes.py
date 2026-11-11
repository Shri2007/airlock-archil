"""Regressions for the Codex review findings (integration, phase2-tree)."""
import tempfile
import unittest

import nodes
import peers
from board import LocalBoard


class ReviewFixes(unittest.TestCase):
    def test_broken_version_load_is_an_error_result_not_a_crash(self):
        world = nodes.all_worlds()[0]
        node = {"id": nodes.node_id(world), "world": world, "variant": None}
        snap = nodes.materialize(node, "local")
        broken = {"id": "vX", "track": "code", "policy": None,
                  "files": {"brain.py": "raise RuntimeError('boom at import')\n"}}
        r = nodes.evaluate(broken, node, snap)
        self.assertTrue(r["output"].startswith("ERROR RuntimeError"))
        self.assertIn(r["verdict"], ("PASS", "BLOCK"))

    def test_eval_defers_when_a_node_appears_without_a_snapshot(self):
        b = LocalBoard(tempfile.mkdtemp(prefix="board_"))
        scout, lab = peers.Scout(), peers.Lab()
        scout.step(b)
        while peers.pending_nodes(b):
            lab.step(b)
        # the attacker adds a node after the lab decided everything was ready
        b.put("nodes/n-111110~A9.json", {"id": "n-111110~A9", "world": nodes.all_worlds()[1],
              "variant": {"id": "A9", "title": "t", "injection": "x"}, "origin": "attack",
              "against": "v0", "state": "pending"})
        self.assertEqual(lab._score(b, "eval.v0"), "defer")
        self.assertIsNone(b.get("evals/v0.json"))
        self.assertNotIn("n-111110~A9", lab._snaps)  # the miss was not cached

    def test_deferred_eval_task_goes_back_to_open(self):
        b = LocalBoard(tempfile.mkdtemp(prefix="board_"))
        scout, lab = peers.Scout(), peers.Lab()
        scout.step(b)
        while peers.pending_nodes(b):
            lab.step(b)
        b.put("nodes/n-111110~A9.json", {"id": "n-111110~A9", "world": nodes.all_worlds()[1],
              "variant": None, "origin": "attack", "against": "v0", "state": "ready"})  # ready but no snapshot
        lab.step(b)
        self.assertTrue(b.exists("tasks/open/eval.v0.json"))
        self.assertEqual(b.list("tasks/claimed"), [])


if __name__ == "__main__":
    unittest.main()
