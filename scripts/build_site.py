#!/usr/bin/env python3
"""Build docs/ for GitHub Pages: a static, read-only replay of saved Airlock runs.

    python3 scripts/build_site.py

Pages serves docs/ (source: main, /docs). The page is the same viewer.html you run
locally, plus saved data. It cannot run the engine or Archil; it replays recorded
runs. Re-run this after a fresh `python3 demo.py live` so the hosted page shows
the latest real-Archil run, then commit docs/.

Safety: every JSON file is scrubbed of Archil sandbox and disk ids, and the build
fails if anything that looks like a real key ends up in docs/.
"""
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

IDS = [(re.compile(r"dsk-[0-9a-f]{6,}"), "dsk-redacted"),
       (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "sandbox-redacted")]
# Real-key shapes. The project's decoys are tagged BAIT-* and never match these.
BAD = re.compile(r"sk-ant-|\bkey-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
                 r"|BEGIN [A-Z ]*PRIVATE KEY|ARCHIL_API_KEY\s*=")


def scrub(text):
    for pattern, repl in IDS:
        text = pattern.sub(repl, text)
    return text


def put(src, dst_rel):
    dst = DOCS / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(scrub(Path(src).read_text()))


def mode_of(path):
    try:
        return json.loads(Path(path).read_text()).get("mode")
    except Exception:
        return None


def main():
    if DOCS.exists():
        for child in DOCS.iterdir():  # keep hand-made files (viewer.png, screenshot.sh)
            if child.name in ("index.html", "results.json", "demo_results", "fixtures", ".nojekyll"):
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    DOCS.mkdir(exist_ok=True)

    shutil.copy(ROOT / "viewer.html", DOCS / "index.html")
    (DOCS / ".nojekyll").write_text("")

    # "Latest run": prefer a real Archil run if one was saved, else the last local run.
    candidates = [ROOT / "results.json", ROOT / "demo_results" / "1-live.json"]
    live = [c for c in candidates if c.exists() and mode_of(c) == "archil"]
    chosen = (live or [c for c in candidates if c.exists()] or [None])[0]
    if chosen is None:
        sys.exit("No results.json or demo_results/1-live.json. Run `python3 demo.py all` first.")
    put(chosen, "results.json")

    copied = []
    for f in sorted((ROOT / "demo_results").glob("*.json")) if (ROOT / "demo_results").exists() else []:
        put(f, f"demo_results/{f.name}")
        copied.append(f.name)
    for name in ("board.snapshot.json", "board.log.jsonl"):
        if (ROOT / "fixtures" / name).exists():
            put(ROOT / "fixtures" / name, f"fixtures/{name}")

    bad = []
    for f in DOCS.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl", ".html", ".md"):
            m = BAD.search(f.read_text(errors="ignore"))
            if m:
                bad.append((f.relative_to(ROOT), m.group(0)[:12] + "..."))
    if bad:
        sys.exit(f"REFUSING: possible real secret in {bad}")

    print(f"docs/ built. latest run: {chosen.relative_to(ROOT)} (mode {mode_of(chosen)})")
    print(f"scenarios: {', '.join(copied) or 'none'}")
    print("secret scan: clean. Preview: python3 -m http.server 8000 --directory docs --bind 127.0.0.1")


if __name__ == "__main__":
    main()
