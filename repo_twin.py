"""Public GitHub text as an inert twin, with sanitized offline fixtures.

Only README text and issue title/body fields are accepted. Repository code,
attachments, filenames and shell commands are never downloaded or executed.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from brain import _ACTION, _AUTHORITY, _SECRET

HERE = Path(__file__).resolve().parent
CACHE = HERE / "twin_cache"
FIXTURES = HERE / "fixtures" / "repos"
MAX_ISSUES = 20
BODY_LIMIT = 1500
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [^\r\n]*PRIVATE KEY-----.*?(?:-----END [^\r\n]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/-]{40,}={0,2}(?![A-Za-z0-9_+/=-])"),
]


def scrub(text):
    """Redact recognizable credential formats before caching or seeding text."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def skip_issue(text):
    """Conservative filter: exclude any offline-brain keyword in real issues."""
    return any(pattern.search(text) for pattern in (_ACTION, _SECRET, _AUTHORITY))


def _gh(endpoint):
    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, timeout=25,
        env=dict(os.environ, GH_PROMPT_DISABLED="1", GIT_TERMINAL_PROMPT="0"),
        check=True,
    )
    return json.loads(result.stdout)


def _sanitize(payload):
    """Whitelist fields and scrub full bodies before truncating them."""
    if not isinstance(payload, dict) or not isinstance(payload.get("readme"), str):
        raise ValueError("missing README text")
    issues = []
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        title, body = issue.get("title") or "", issue.get("body") or ""
        if not isinstance(title, str) or not isinstance(body, str):
            continue
        issues.append({"number": int(issue["number"]),
                       "title": scrub(title)[:300], "body": scrub(body)[:BODY_LIMIT]})
        if len(issues) == MAX_ISSUES:
            break
    return {"readme": scrub(payload["readme"])[:100_000], "issues": issues,
            "fetched_at": str(payload.get("fetched_at", "unknown"))}


def _fetch(name):
    metadata = _gh(f"repos/{name}")
    # Verify before downloading content, even if gh has access to private repos.
    if metadata.get("private") is not False or metadata.get("visibility", "public") != "public":
        raise ValueError("only public repositories are supported")
    readme = _gh(f"repos/{name}/readme")
    if readme.get("encoding") != "base64":
        raise ValueError("unsupported README encoding")
    issues = []
    for page in range(1, 4):
        batch = _gh(f"repos/{name}/issues?state=all&per_page=100&page={page}")
        issues.extend(issue for issue in batch if "pull_request" not in issue)
        if len(issues) >= MAX_ISSUES or len(batch) < 100:
            break
    return _sanitize({
        "readme": base64.b64decode(readme["content"]).decode("utf-8", errors="replace"),
        "issues": issues,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@dataclass(frozen=True)
class RepoTwin:
    root: Path
    issue_texts: tuple[str, ...]
    metadata: dict

    @contextmanager
    def activate(self):
        previous = os.environ.get("AIRLOCK_TWIN")
        os.environ["AIRLOCK_TWIN"] = str(self.root)
        try:
            yield self
        finally:
            if previous is None:
                os.environ.pop("AIRLOCK_TWIN", None)
            else:
                os.environ["AIRLOCK_TWIN"] = previous


def load_twin(name, offline=False, keep_all=False, *, cache_dir=CACHE, fixtures_dir=FIXTURES):
    """Fetch a public repo, falling back to a committed, scrubbed fixture."""
    if not isinstance(name, str) or not _REPO.fullmatch(name) or name.split("/")[1] in (".", ".."):
        raise ValueError("repo must be owner/name")
    name = name.lower()
    slug = name.replace("/", "__")
    source = "fixture"
    payload = None
    if not offline:
        try:
            payload = _fetch(name)
            source = "live"
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            # Don't print gh stderr: remote content and credentials are untrusted.
            print(f"  ! public repo fetch unavailable for {name}; trying offline fixture")
    if payload is None:
        fixture = Path(fixtures_dir) / slug / "repo.json"
        if not fixture.is_file():
            raise ValueError(f"no offline fixture for {name}; connect gh to fetch the public repo")
        payload = _sanitize(json.loads(fixture.read_text()))
    texts, skipped = [], 0
    for issue in payload["issues"]:
        text = f"Title: {issue['title']}\n\n{issue['body']}\n"
        if not keep_all and skip_issue(text):
            skipped += 1
        else:
            texts.append(text)
    metadata = {"name": name, "source": source, "issues_used": len(texts),
                "issues_skipped": skipped, "fetched_at": payload["fetched_at"]}
    # Content-addressed seeds avoid stale issues and keep concurrent runs separate.
    serialized = json.dumps(payload, sort_keys=True)
    seed_content = json.dumps({"readme": payload["readme"], "issues": texts}, sort_keys=True)
    digest = hashlib.sha256(seed_content.encode()).hexdigest()[:20]
    cache = Path(cache_dir) / slug
    root = cache / digest
    cache.mkdir(parents=True, exist_ok=True)
    # Publish complete seed directories, so a parallel run cannot see half a twin.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=cache) as staging:
        staged_root = Path(staging) / "seed"
        issues_dir = staged_root / "issues"
        issues_dir.mkdir(parents=True)
        (staged_root / "README.md").write_text(payload["readme"])
        for index, text in enumerate(texts):
            filename = "1.md" if index == 0 else f"f{index:02d}.md"
            (issues_dir / filename).write_text(text)
        try:
            staged_root.rename(root)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not root.is_dir():
                raise
            # An identical, complete content-addressed twin already exists.
        manifest = Path(staging) / "repo.json"
        manifest.write_text(serialized + "\n")
        manifest.replace(cache / "repo.json")
    print(f"REPO: {name} ({source}) | {len(texts)} issues used, {skipped} skipped")
    return RepoTwin(root, tuple(texts), metadata)
