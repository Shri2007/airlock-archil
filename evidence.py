"""The evidence locker: ONE shared disk that every forked clone reports into.

  ledger.jsonl        one line per clone (append_object, all clones share it)
  events/<run>.jsonl  that clone's full audit trail
  outbox/<run>.txt    anything the tool tried to send out over the network.
                      The "attacker's server" is just a folder on our disk.

After the search, the judge's verdicts are double-checked by grepping the whole
outbox for canary tags: disk.grep("BAIT-"). Two independent signals have to agree.

Local:  ./evidence/<session>/...      (plain folder)
Archil: disk "airlock-evidence", <session>/...   (append_object / put_object / grep)
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

DISK_NAME = "airlock-evidence"
CANARY = "BAIT-"


class LocalLocker:
    kind = "local folder"

    def __init__(self, session):
        self.session = session
        self.root = Path(__file__).parent / "evidence" / session
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.where = str(self.root.relative_to(Path(__file__).parent))

    def put(self, key, text):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def append(self, key, line):
        with self.lock:
            p = self.root / key
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(line)

    def grep(self, folder, pattern):
        hits, rx = [], re.compile(re.escape(pattern))
        base = self.root / folder
        for p in sorted(base.rglob("*")) if base.exists() else []:
            if p.is_file():
                for line in p.read_text().splitlines():
                    if rx.search(line):
                        hits.append((str(p.relative_to(self.root)), line))
        return hits


class ArchilLocker:
    kind = "archil disk"

    def __init__(self, session):
        import archil
        self.session = session
        found = archil.list_disks(name=DISK_NAME)
        self.disk = found[0] if found else archil.create_disk(name=DISK_NAME).disk
        self.where = f"archil disk {DISK_NAME} ({self.disk.id}) / {session}"
        self.lock = threading.Lock()

    def _key(self, key):
        return f"{self.session}/{key}"

    def put(self, key, text):
        self.disk.put_object(self._key(key), text.encode(), "text/plain")

    def append(self, key, line):
        # Parallel clones share one ledger. Archil rejects racing appends with a
        # 409 ("retry the append"), so serialize ours and retry with backoff.
        from archil import ArchilS3Error
        with self.lock:
            for attempt in range(6):
                try:
                    self.disk.append_object(self._key(key), line.encode(), "text/plain")
                    return
                except ArchilS3Error as e:
                    if "409" not in str(e) or attempt == 5:
                        raise
                    time.sleep(0.2 * (attempt + 1))

    def grep(self, folder, pattern):
        res = self.disk.grep(directory=self._key(folder), pattern=re.escape(pattern),
                             recursive=True)
        hits = []
        for m in res.matches:
            f = m.file.lstrip("/")
            f = f.split(f"{self.session}/", 1)[-1]  # normalize to session-relative
            hits.append((f, m.text.strip()))
        return hits


def make_locker():
    # Unique per run: two runs in the same second must never share an outbox,
    # or grep would count the previous run's leaks.
    session = time.strftime("run-%Y%m%d-%H%M%S") + f"-{os.getpid()}-{time.time_ns() % 10**6:06d}"
    if os.environ.get("USE_ARCHIL") == "1":
        return ArchilLocker(session)
    return LocalLocker(session)
