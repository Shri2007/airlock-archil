"""One behavioral suite, run against LocalBoard and ArchilBoard-on-fake."""
import tempfile
import threading
import unittest

from archil_fake import FakeArchil
from board import LocalBoard, build_snapshot
from board_archil import ArchilBoard

SNAP_KEYS = {"control", "scan", "nodes", "versions", "evals", "tasks",
             "champion", "memory", "log_len"}


class BoardSuite:
    def make(self):
        raise NotImplementedError

    def setUp(self):
        self.b = self.make()

    def test_put_get_exists_list_delete(self):
        self.b.put("nodes/n-111110.json", {"id": "n-111110"})
        self.b.put("nodes/n-000000.json", {"id": "n-000000"})
        self.b.put("versions/v0.json", {"id": "v0"})
        self.assertEqual(self.b.get("nodes/n-111110.json"), {"id": "n-111110"})
        self.assertTrue(self.b.exists("nodes/n-111110.json"))
        self.assertFalse(self.b.exists("nodes/nope.json"))
        self.assertIsNone(self.b.get("nodes/nope.json"))
        self.assertEqual(self.b.list("nodes"), ["nodes/n-000000.json", "nodes/n-111110.json"])
        self.assertEqual(self.b.list("nodes/"), ["nodes/n-000000.json", "nodes/n-111110.json"])
        self.assertEqual(self.b.list("missing"), [])
        self.b.put("nodes/n-111110.json", {"id": "n-111110", "state": "ready"})
        self.assertEqual(self.b.get("nodes/n-111110.json")["state"], "ready")
        self.b.delete("nodes/n-111110.json")
        self.b.delete("nodes/n-111110.json")  # idempotent
        self.assertEqual(self.b.list("nodes"), ["nodes/n-000000.json"])

    def test_claim_exactly_one_winner_under_threads(self):
        self.b.put("tasks/open/eval.v0.json", {"kind": "eval"})
        wins, go = [], threading.Barrier(8)
        def grab(i):
            go.wait()
            if self.b.claim("tasks/open/eval.v0.json", f"tasks/claimed/eval.v0.lab-{i}.json"):
                wins.append(i)
        ts = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1)
        self.assertEqual(self.b.list("tasks/open"), [])
        self.assertEqual(self.b.list("tasks/claimed"), [f"tasks/claimed/eval.v0.lab-{wins[0]}.json"])
        self.assertEqual(self.b.get(f"tasks/claimed/eval.v0.lab-{wins[0]}.json"), {"kind": "eval"})
        self.assertFalse(self.b.claim("tasks/open/eval.v0.json", "tasks/claimed/x.json"))

    def test_log_seq_and_since(self):
        self.b.log("scout#1", "node", id="n-1")
        self.b.log("lab#1", "eval", version="v0")
        log = self.b.read_log()
        self.assertEqual([e["seq"] for e in log], [0, 1])
        self.assertEqual(log[0]["agent"], "scout#1")
        self.assertEqual(log[0]["id"], "n-1")
        self.assertIn("ts", log[0])
        self.assertEqual(self.b.read_log(since=1)[0]["event"], "eval")

    def test_log_threads_seq_contiguous(self):
        ts = [threading.Thread(target=self.b.log, args=(f"a#{i}", "x")) for i in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual([e["seq"] for e in self.b.read_log()], list(range(10)))

    def test_memory_and_reset_keeps_memory(self):
        self.b.remember("fixes", {"title": "x"})
        self.b.remember("fixes", {"title": "y"})
        self.b.put("nodes/n-1.json", {"id": "n-1"})
        self.b.put("tasks/open/a.json", {})
        self.b.log("a#1", "x")
        self.b.reset_run()
        self.assertEqual(self.b.list("nodes"), [])
        self.assertEqual(self.b.list("tasks"), [])
        self.assertEqual(self.b.read_log(), [])
        self.assertEqual(self.b.recall("fixes"), [{"title": "x"}, {"title": "y"}])
        self.assertEqual(self.b.recall("attacks"), [])

    def test_snapshot_shape_and_champion(self):
        empty = self.b.snapshot()
        self.assertEqual(set(empty), SNAP_KEYS)
        self.assertEqual(empty["tasks"], {"open": [], "claimed": [], "done": []})
        self.assertIsNone(empty["champion"])
        self.b.put("nodes/n-1.json", {"id": "n-1"})
        self.b.put("versions/v0.json", {"id": "v0", "track": "base", "generation": 0, "patch": ""})
        self.b.put("evals/v0.json", {"version": "v0", "passes": 1, "utility": True, "nodes_seen": 1})
        self.b.put("tasks/open/eval.v0.json", {})
        self.b.put("control/approved.json", {"by": "cli", "ts": 0})
        self.b.remember("attacks", {"id": "A1"})
        self.b.log("scout#1", "node")
        s = self.b.snapshot()
        self.assertEqual(s["champion"], "v0")
        self.assertEqual(s["tasks"]["open"], ["eval.v0.json"])
        self.assertEqual(s["memory"], {"fixes": 0, "attacks": 1})
        self.assertEqual(s["log_len"], 1)
        self.assertEqual(s["control"]["approved"]["by"], "cli")
        self.assertEqual(s, build_snapshot(self.b))


class LocalBoardBehavior(BoardSuite, unittest.TestCase):
    def make(self):
        return LocalBoard(tempfile.mkdtemp(prefix="board_"))


class ArchilBoardOnFake(BoardSuite, unittest.TestCase):
    def make(self):
        self.fake = FakeArchil(racy=True)
        return ArchilBoard(client=self.fake)

    def test_reuses_existing_disk(self):
        self.b.put("x/a.json", {"a": 1})
        again = ArchilBoard(client=self.fake)
        self.assertIs(again.disk, self.b.disk)
        self.assertEqual(again.get("x/a.json"), {"a": 1})

    def test_append_retries_409(self):
        self.b.disk.fail_appends = 3
        self.b.log("a#1", "x")
        self.assertEqual(len(self.b.read_log()), 1)
        self.assertEqual(self.b.disk.append_409s, 3)

    def test_append_gives_up_eventually(self):
        self.b.disk.fail_appends = 100
        with self.assertRaises(Exception):
            self.b.remember("fixes", {"t": 1})

    def test_two_processes_racing_appends(self):
        other = ArchilBoard(client=self.fake)  # separate lock = separate process
        def spam(b, tag):
            for i in range(10):
                b.remember("attacks", {"by": tag, "i": i})
        ts = [threading.Thread(target=spam, args=(b, t)) for b, t in ((self.b, "a"), (other, "b"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.b.recall("attacks")), 20)

    def test_claim_uses_conditional_mount(self):
        seen = []
        real = self.fake.exec
        def spy(**kw):
            seen.append(kw["disks"]["b"])
            return real(**kw)
        self.fake.exec = spy
        self.b.put("tasks/open/t.json", {})
        self.assertTrue(self.b.claim("tasks/open/t.json", "tasks/claimed/t.a-1.json"))
        self.assertTrue(seen[0].conditional)


if __name__ == "__main__":
    unittest.main()
