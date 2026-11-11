"""Local demo server: enter a public repo, run Airlock, reload its results.

    python3 serve.py --port 8001
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid

HERE = Path(__file__).resolve().parent


def repo_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a public GitHub repository as owner/name.")
    name = value.strip()
    if name.startswith("https://github.com/"):
        parsed = urlsplit(name)
        if parsed.netloc != "github.com" or parsed.query or parsed.fragment:
            raise ValueError("Use a GitHub repository URL without query parameters.")
        name = parsed.path.strip("/")
        if name.endswith(".git"):
            name = name[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", name) or name.split("/")[-1] in (".", ".."):
        raise ValueError("Enter owner/name or https://github.com/owner/name.")
    return name.lower()


class ScanManager:
    def __init__(self, root=HERE):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.job = None
        self.started = None

    def snapshot(self):
        with self.lock:
            if not self.job:
                return {"state": "idle"}
            job = dict(self.job)
            if job["state"] == "running" and self.started is not None:
                job["elapsed_seconds"] = int(time.monotonic() - self.started)
            return job

    def start(self, repo, mode, offline):
        with self.lock:
            if self.job and self.job["state"] == "running":
                raise RuntimeError("A scan is already running. Wait for it to finish.")
            self.started = time.monotonic()
            self.job = {"id": uuid.uuid4().hex, "repo": repo, "mode": mode,
                        "state": "running", "progress": "Preparing repository…", "log": []}
            job = dict(self.job)
        threading.Thread(target=self._run, args=(repo, mode, offline), daemon=True).start()
        return job

    def _run(self, repo, mode, offline):
        command = [sys.executable, "-u", str(self.root / "run_airlock.py"),
                   "--repo", repo, "--profile", "deferential"]
        if offline:
            command.append("--repo-offline")
        env = dict(os.environ, USE_ARCHIL="1" if mode == "archil" else "0",
                   AGENT_BRAIN="offline", PYTHONUNBUFFERED="1")
        try:
            with subprocess.Popen(command, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) as process:
                for line in process.stdout:
                    # Show scanner progress, without forwarding SDK error bodies.
                    text = line.strip()
                    if text.startswith("AIRLOCK  |"):
                        progress = ("Provisioning the base Archil microVM and forking the first worlds…"
                                    if "archil sandbox" in text else "Running the local search…")
                        with self.lock:
                            self.job["progress"] = progress
                    if text.startswith(("REPO:", "ROUND ", "Found the betrayal", "No betrayal found", "EVIDENCE:", "Finishing the SAME search", "CRIME SCENE KEPT:")):
                        with self.lock:
                            self.job["progress"] = text
                            self.job["log"] = (self.job["log"] + [text])[-20:]
                if process.wait() != 0:
                    raise RuntimeError("Scan failed. Check the repo name, GitHub access, and Archil configuration, then retry.")
            result = json.loads((self.root / "results.json").read_text())
            if result.get("repo", {}).get("name") != repo:
                raise RuntimeError("The scanner did not produce results for this repository.")
            with self.lock:
                fallback = mode == "archil" and result.get("mode") != "archil"
                self.job.update(state="fallback" if fallback else "complete",
                                elapsed_seconds=int(time.monotonic() - self.started),
                                progress="Archil failed. Showing local fallback results." if fallback
                                else "Scan complete. Loading results…",
                                result_mode=result.get("mode"), repo_info=result["repo"],
                                note=result.get("note"))
        except Exception as error:
            with self.lock:
                self.job.update(state="failed", progress=str(error) if isinstance(error, RuntimeError)
                                else "Could not complete the scan. Check the server terminal and retry.")


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, manager=None):
        self.scans = manager or ScanManager()
        super().__init__(address, Handler)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(args[2].scans.root), **kwargs)

    def _local_request(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin == f"http://{host}")

    def _json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._local_request():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Use the local viewer to start a scan."})
        if self.path != "/api/scan":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint."})
        if self.headers.get_content_type() != "application/json":
            return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send JSON."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length))
            repo = repo_name(body.get("repo"))
            mode = body.get("mode", "local")
            offline = body.get("offline", False)
            if mode not in ("local", "archil") or type(offline) is not bool:
                raise ValueError("Choose local or Archil mode.")
            job = self.server.scans.start(repo, mode, offline)
        except (ValueError, AttributeError) as error:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            return self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        self._json(HTTPStatus.ACCEPTED, job)

    def do_GET(self):
        if not self._local_request():
            return self.send_error(HTTPStatus.FORBIDDEN)
        if self.path == "/api/scan":
            return self._json(HTTPStatus.OK, self.server.scans.snapshot())
        return super().do_GET()

    def send_head(self):
        # Serve viewer assets and generated reports, never keys or source trees.
        path = unquote(urlsplit(self.path).path)
        parts = Path(path).parts
        if ".." in parts or any(part.startswith(".") for part in parts):
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        relative = path.lstrip("/") or "viewer.html"
        allowed = relative in ("viewer.html", "results.json") or (
            relative.startswith(("demo_results/", "fixtures/", "out/", "docs/"))
            and Path(relative).suffix in (".json", ".jsonl", ".png", ".css", ".js"))
        target = (self.server.scans.root / relative).resolve()
        if not allowed or not target.is_relative_to(self.server.scans.root.resolve()):
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        self.path = "/" + relative
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = DemoServer(("127.0.0.1", args.port))
    print(f"Airlock demo: http://127.0.0.1:{server.server_port}/viewer.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
