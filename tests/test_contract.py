"""Contract smoke tests for LocalBoard (CONTRACTS.md section 1)."""
import tempfile
import threading
import unittest

from board import LocalBoard, open_board


class LocalBoardContract(unittest.TestCase):
    def setUp(self):
        self.b = LocalBoard(tempfile.mkdtemp(prefix="board_"))

    def test_put_get_list_delete(self):
        self.b.put("findings/F1.json", {"id": "F1"})
        self.assertEqual(self.b.get("findings/F1.json"), {"id": "F1"})
        self.assertEqual(self.b.list("findings"), ["findings/F1.json"])
        self.b.delete("findings/F1.json")
        self.assertIsNone(self.b.get("findings/F1.json"))

    def test_claim_has_exactly_one_winner(self):
        self.b.put("tasks/open/fix-code.F1.json", {"kind": "fix-code"})
        wins = []
        def grab(i):
            if self.b.claim("tasks/open/fix-code.F1.json", f"tasks/claimed/fix-code.F1.a-{i}.json"):
                wins.append(i)
        ts = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1)

    def test_log_seq_and_since(self):
        self.b.log("scout#1", "finding", id="F1")
        self.b.log("lab#1", "verdict", candidate="C1")
        self.assertEqual([e["seq"] for e in self.b.read_log()], [0, 1])
        self.assertEqual(self.b.read_log(since=1)[0]["event"], "verdict")

    def test_reset_keeps_memory(self):
        self.b.remember("fixes", {"title": "x"})
        self.b.put("findings/F1.json", {"id": "F1"})
        self.b.reset_run()
        self.assertEqual(self.b.list("findings"), [])
        self.assertEqual(self.b.recall("fixes"), [{"title": "x"}])

    def test_snapshot_shape(self):
        snap = self.b.snapshot()
        for k in ("control", "scan", "nodes", "versions", "evals", "tasks",
                  "champion", "memory", "log_len"):
            self.assertIn(k, snap)

    def test_champion_rule(self):
        from board import champion
        vs = [{"id": "v0", "track": "base", "generation": 0, "patch": ""},
              {"id": "v1.a", "track": "code", "generation": 1, "patch": "xx"},
              {"id": "v1.p1", "track": "policy", "generation": 1, "patch": ""},
              {"id": "v2.a", "track": "code", "generation": 2, "patch": "xxxx"}]
        ev = lambda v, p, u=True, seen=66: {"version": v, "passes": p, "utility": u, "nodes_seen": seen}
        self.assertEqual(champion(vs, [ev("v0", 62), ev("v1.a", 64), ev("v1.p1", 66), ev("v2.a", 66)], 66), "v2.a")
        self.assertEqual(champion(vs, [ev("v0", 62), ev("v1.a", 64), ev("v2.a", 66, u=False)], 66), "v1.a")
        self.assertEqual(champion(vs, [ev("v0", 62), ev("v1.a", 64, seen=64)], 66), "v0")

    def test_open_board_default_local(self):
        self.assertEqual(open_board(root=self.b.root).kind, "local")


if __name__ == "__main__":
    unittest.main()
