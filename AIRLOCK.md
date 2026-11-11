# Airlock — read this first

We built this for the Archil hackathon. This file explains the whole idea in
plain English and how the code works. Setup and architecture details are in
[README.md](README.md); the Phase 2 spec is in [CONTRACTS.md](CONTRACTS.md).

---

## The idea in one line

**Most people test an AI tool once and hope it's safe. Airlock clones the
computer many times — each a little different — finds the exact situation
where the tool turns against you, and then (if you say yes) sends a swarm of
agents to fix it and prove the fix holds.**

## Why this matters (the problem)

AI now writes the little "tools" that other AIs use — tools that can open files,
hit the internet, run commands. People test a new tool once, it looks fine, they
ship it. But a tool can behave perfectly in one setup and go rogue in a slightly
different one (different data lying around, a different permission, the internet
on vs off). Testing once and saying "safe" is like test-driving a car only on a
sunny day and calling it safe in all weather.

## What Airlock does

1. Take the AI tool you want to vet.
2. **Clone the environment many ways.** Each clone differs in a few knobs (a
   secret file present or not, internet on or off, a booby-trapped issue that
   claims to come from the repo owner or not, ...).
3. Run the same tool in every clone and watch what it touches.
4. A dumb-but-honest checker (plain code, no AI) scores each clone 0-5 and
   flags any clone where the tool actually stole a (fake, tagged) secret.
5. **Report the exact trigger:** not "pass/fail", but *"it only betrays you when
   the poisoned issue claims authority AND a secret is present AND the internet
   is on AND the repo is small; the CI file doesn't matter."*
6. **Fix it, with your permission.** Every setting of the world (all 64 knob
   combinations) gets its own sandbox. Fixer agents write new versions of the
   tool, an attacker agent adds harder settings that beat the best version,
   and every version is scored on every setting. The versions grow into a
   tree; the one that passes the most settings is the champion. You get its
   patch and a sandbox policy as files.

## Why it wins for Archil

Archil forks sandboxes in seconds and gives agents disks they can all share.
Airlock uses state the three ways Archil is built for:

- **Forked:** every world is a fork, and the machine that betrayed you stays
  paused so you can fork it again and ask "what if".
- **Persistent:** the betraying machine stays paused as forkable evidence, and
  the swarm's memory (known bypasses, proven fixes) carries over to the next run.
- **Shared:** every clone files its trail on one shared Archil disk, and the
  swarm has no boss: its agents coordinate through a shared board disk, claiming
  work with atomic renames.

That is what Archil asked to see: sandboxes forked to try ideas in parallel,
and agents sharing disks.

## The demo (what the judge sees, ~60s)

1. "Here's a new AI tool. We don't trust it."
2. "Watch." → clones appear and the search hunts through them round by round.
3. A few flash **RED**. One line prints the exact trigger.
4. "What if?" → fork the paused machine that leaked, cut the network, and the
   attack stops at step 4.
5. "Fix it" → the tree view shows fixer agents writing new versions of the tool,
   an attacker beating the first fix, and a second fix that holds.

## How the code works

Run it: `python3 run_airlock.py` (local, free) or `USE_ARCHIL=1 python3 run_airlock.py` (real Archil forks).

**Phase 1, the scan:**

| File | What it is |
|---|---|
| `twin/` | the base environment (fake repo with issues + a README) |
| `tags.py` | the fake-secret tags (if one leaves a clone, we caught a theft) |
| `workspace.py` | one environment. `branch()` clones it; local folder or a real Archil sandbox |
| `recorder.py` | logs everything the tool does in each clone |
| `brain.py` | the AI tool's "decision" (offline by default; real Haiku optional) |
| `tool_under_test.py` | the tool we're vetting (a summarizer that *might* be tricked) |
| `environments.py` | the 6 knobs, and how to turn a fork into one specific world |
| `judge.py` | the plain-code checker: scores 0-5, BLOCK when a secret left |
| `search.py` | explore / climb / shrink: find the betrayal in ~15 of 64 worlds |
| `run_airlock.py` | ties it together, prints each round, writes `results.json` |
| `evidence.py` | the shared Archil disk every clone reports to, and the `grep` check against the judge |
| `crime.py` | keeps the betraying machine paused; fork it to replay or ask "what if" |
| `demo.py` | the stage scenarios (four tool profiles, a real repo, live on Archil) |
| `repo_twin.py` | uses a real public GitHub repo's README and issues as the world |
| `serve.py` | local server that adds a "Scan repo" box to the viewer |

**Phase 2, the fixer swarm:**

| File | What it is |
|---|---|
| `board.py` | the shared board every agent reads and writes (local folder or Archil disk) |
| `peers.py` | the agents: scout, fixer-code, fixer-policy, attacker, lab |
| `swarm.py` | launches the agents, writes approval, builds the report and outputs |
| `nodes.py` | one sandbox per world, and scoring any version of the tool on any world |
| `viewer.html` | the viewer: round-by-round replay, a plain-English guide, and the swarm's version tree |

## Rules (keep these true)

- Pure Python, runs offline for $0. The real model and real Archil are optional
  upgrades.
- Every secret is fake and tagged (`BAIT-*`). Never put a real secret in `twin/`.
- Network-facing code binds to `127.0.0.1` only. Don't commit `audit.jsonl`.
- The checker (`judge.py`) stays plain code — no AI decides pass/block.
- Nothing is applied to your code without your approval.

## The pitch line to practice

> "Everyone tests an AI tool once and hopes. We fork a machine dozens of ways on
> Archil, find the exact conditions where it betrays you, and keep the machine
> it leaked on so you can fork it and ask what if. Then a swarm of agents
> proposes fixes and attacks them."
