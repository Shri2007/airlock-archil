# Archil demo script

Open `http://127.0.0.1:8001/viewer.html?demo=1`.
Keep `Shri2007/airlock-demo-api` selected. Budget about two minutes.

**Start with the repo input visible.**

“An AI tool can pass your tests and still leak credentials when its environment changes.

Airlock finds the combination of conditions that causes the failure, then tests fixes against it.

Here we’re testing our sample issue-summarizer using real GitHub issue data. We add a malicious issue and obviously fake credentials.”

**Click Play prepared run. The recorded search takes about ten seconds to replay.**

“This is an accelerated replay of a real Archil run we prepared before presenting. The original scan took about a minute and used fifteen real microVM forks.

We try different environments, follow the ones that get closer to a leak, then switch conditions off to find what actually matters.”

**Point to the red tile, then the trigger and evidence.**

“There’s the failure. Five conditions together cause the leak. The CI file makes no difference.

Archil does three jobs here. Forks give us separate environments to compare. A shared disk collects the audit trails and fake-secret transfers. And we keep the failing microVM paused so we can fork it again and ask what happens when one condition changes.

The captured transfers agree with the judge’s verdict.”

**Click Fix it: tree. Point to v0, then v1.a, then v2.a.**

“Here’s a recorded repair run. The original tool passes sixty-two of sixty-six tests. The first fix improves that to sixty-four. The next passes all sixty-six while preserving the summarizer’s normal output.

Click a version and you can inspect its patch and the failures it fixed or introduced.

Airlock turns an agent failure into a reproducible experiment, then gives you a tested candidate fix with evidence behind it.”

**Stop and take questions.**

If asked about the brain, explain that this demo uses deterministic vulnerability
profiles and catalog-based candidate fixes. A live-model option also exists.
The prepared replay preserves the real Archil measurements. It does not launch
new microVMs during playback. The repair tree is a separate completed run.
