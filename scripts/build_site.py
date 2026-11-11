#!/usr/bin/env python3
"""Build docs/ for GitHub Pages: a static, read-only replay of saved Airlock runs.

    python3 scripts/build_site.py

Pages serves docs/ (source: main, /docs). The page is the same viewer.html you run
locally, plus saved data. It cannot run the engine or Archil; it replays recorded
runs. Re-run this after a fresh `python3 demo.py live` so the hosted page shows
the latest real-Archil run, then commit docs/.

The hosted page differs from the local viewer in two ways: the "Scan repo" bar is
hidden (it needs serve.py) and a one-line note says these are recorded runs. The
crime scene's sandbox ids are dropped, because that machine is long gone by the
time anyone opens the page.

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


def put(src, dst_rel, drop_ids=False):
    dst = DOCS / dst_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = Path(src).read_text()
    if drop_ids:  # a recorded page must not claim a paused machine that no longer exists
        data = json.loads(text)
        scene = data.get("crime_scene")
        if isinstance(scene, dict):
            scene.pop("sandbox_id", None)
            scene.pop("base_id", None)
        text = json.dumps(data, indent=2)
    dst.write_text(scrub(text))


NOTE = ('<p class="hosted-note" style="margin:0 0 4px;color:#8b9a92;font-size:14px;line-height:1.5">'
        'Recorded runs, replay only. Press <b>R</b> to replay, <b>H</b> for the guide. '
        'The Scan repo box and live Archil runs need the local server: '
        '<a href="https://github.com/Shri2007/airlock-archil" style="color:#3ddc84">github.com/Shri2007/airlock-archil</a>.</p>')
PANEL = re.compile(r'<section class="repo-panel"')


def hosted_page(src, dst):
    """Copy the viewer, hide the scan bar (needs serve.py) and say these are recordings."""
    html = Path(src).read_text()
    if PANEL.search(html):
        html = PANEL.sub(NOTE + '\n  <section class="repo-panel" style="display:none"', html, count=1)
    else:
        print("warning: scan bar markup not found; hosted page left unchanged")
    dst.write_text(html)


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

    hosted_page(ROOT / "viewer.html", DOCS / "index.html")
    (DOCS / ".nojekyll").write_text("")

    # "Latest run": a real Archil run if one was saved (it makes the page say
    # "real Archil microVM forks"), else the clean default scenario, else results.json.
    order = [ROOT / "results.json", ROOT / "demo_results" / "1-live.json"]
    live = [c for c in order if c.exists() and mode_of(c) == "archil"]
    fallback = [c for c in (ROOT / "demo_results" / "1.json", ROOT / "results.json") if c.exists()]
    chosen = (live or fallback or [None])[0]
    if chosen is None:
        sys.exit("No results.json or demo_results/1.json. Run `python3 demo.py all` first.")
    put(chosen, "results.json", drop_ids=True)

    copied = []
    for f in sorted((ROOT / "demo_results").glob("*.json")) if (ROOT / "demo_results").exists() else []:
        put(f, f"demo_results/{f.name}", drop_ids=True)
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
