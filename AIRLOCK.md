# Airlock — read this first (for the team)

We're building this for the Archil hackathon. This file explains the whole idea
in plain English, how the code works, and who builds what. Tomorrow we copy this
folder into a fresh repo and go.

---

## The idea in one line

**Most people test an AI tool once and hope it's safe. Airlock clones the
computer many times — each a little different — and finds the exact situation
where the tool turns against you.**

## Why this matters (the problem)

AI now writes the little "tools" that other AIs use — tools that can open files,
hit the internet, run commands. People test a new tool once, it looks fine, they
ship it. But a tool can behave perfectly in one setup and go rogue in a slightly
different one (different data lying around, a different permission, the internet
on vs off). Testing once and saying "safe" is like test-driving a car only on a
sunny day and calling it safe in all weather.

## What Airlock does

1. Take the AI tool you want to vet.
2. **Clone the environment lots of ways at once** — each clone changes ONE thing
   (one has a secret file, one doesn't; one has the internet on, one off; one has
   a booby-trapped message in it, one doesn't).
3. Run the same tool in every clone and watch what it touches.
4. A dumb-but-honest checker (plain code, no AI) flags any clone where the tool
   actually stole a (fake, tagged) secret.
5. **Report the exact trigger:** not "pass/fail", but *"it only betrays you when
   there's a secret present AND the internet is on AND a message tried to trick
   it."*

## Why it wins for Archil

The cloning is the magic, and it's the one thing Archil is uniquely great at:
forking a whole machine in ~35 milliseconds, so you can spin up a wall of
slightly-different computers for basically free. Nobody else in the room can
cheaply do that. That's the "Best Use of Archil" win. The demo (a grid of
machines, mostly green, a few flashing red, plus the trigger line) is the wow.

## The demo (what the judge sees, ~60s)

1. "Here's a new AI tool. We don't trust it."
2. "Watch." → a grid of environments appears, the tool runs in each.
3. Most glow green. A few flash **RED**. → "It betrays you — but only here."
4. One line prints: *"betrays only when secret + network + injected are all
   true."* → "We found the exact condition, automatically, by cloning reality."

## How the code works (current skeleton — it runs offline, $0)

Run it: `python3 run_airlock.py`

| File | What it is |
|---|---|
| `twin/` | the base environment (fake repo with issues + a README) |
| `tags.py` | the fake-secret tags (if one leaves a clone, we caught a theft) |
| `workspace.py` | one environment. `branch()` clones it. **The only Archil swap point** |
| `recorder.py` | logs everything the tool does in each clone |
| `brain.py` | the AI tool's "decision" (offline by default; real Haiku optional) |
| `tool_under_test.py` | the tool we're vetting (a summarizer that *might* be tricked) |
| `environments.py` | **the engine**: the knobs, and how to make each clone differ |
| `judge.py` | the plain-code checker: did a secret leave? BLOCK vs PASS |
| `search.py` | run the tool in every clone, then find the shared trigger |
| `run_airlock.py` | ties it together and prints the grid |

Right now there are 3 knobs → 8 clones. Only 1 of the 8 betrays, and Airlock
finds that it's the `injected + secret + network` combo.

## What we build tomorrow (priority order)

1. **The grid visual.** The terminal grid is fine to start, but the wow is a
   real on-screen grid of machines lighting up green/red live. This is the #1
   thing judges remember — spend real time here.
2. **Scale from 8 to ~50 clones.** Add more knobs in `environments.py` (a
   permissions file, a fake chat history, which "connected service" reply is
   present, some random junk files). More knobs = a bigger, more impressive wall.
3. **Make it run on real Archil.** Swap `make_workspace()` / `branch()` to real
   Archil sandboxes + branches so the clones are real microVMs. **Ask the Archil
   team on-site to help with this first thing — and ask them what would make them
   call it the best use of branching.** (Keep the local version as a fallback.)
4. **Turn on the real model** (`AGENT_BRAIN=anthropic`) so the tool genuinely
   gets tricked, not scripted. Offline stays as the safety net.

## Who builds what

- **Shri: the engine (~65%).** Real Archil wiring, the real Haiku model, more
  knobs in `environments.py`.
- **Vishnu: the show (~35%).** The live grid viewer (`viewer.html`), the demo
  script, the backup video, and the submission text.
- The two halves only connect through `results.json`. Full task list and
  timeline are in [README.md](README.md).

## Rules (keep these true)

- Pure Python, runs offline for $0 as the fallback. Real model + real Archil are
  upgrades, each with a fallback.
- Every secret is fake and tagged (`BAIT-*`). Never put a real secret in `twin/`.
- Network-facing code binds to `127.0.0.1` only. Don't commit `audit.jsonl`.
- The checker (`judge.py`) stays plain code — no AI decides pass/block.

## The pitch line to practice

> "Everyone tests an AI tool once and hopes. We clone the machine fifty ways and
> find the exact conditions where it betrays you — something only possible
> because Archil forks a whole computer in milliseconds."
