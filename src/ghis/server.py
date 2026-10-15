"""The ghis web app.

Hosted mode serves strangers: an allowlist, https-only clones, temporary clones.
Personal mode (bound to loopback, so only this computer can connect) also reads local
folders, clones over ssh, keeps the clone cache and saves each story to the data folder."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import git as gitmod
from .assets import version_refs
from .collect import collect
from .common import (
    DATA_DIR,
    SCHEMA_VERSION,
    WEB_DIR,
    CollectError,
    _sink,
    die,
    log,
    same_host,
    tilde,
)
from .demo import make_demo
from .detect import detect_identity, discover_cached, find_token, suggest_url
from .git import HOME_DEPTH
from .providers import COLLECTORS, PROVIDERS
from .site import write_site

DEFAULT_ALLOWED_HOSTS = "github.com,gitlab.com,codeberg.org,bitbucket.org,git.sr.ht"


STATIC = {
    "/": ("connect.html", "text/html; charset=utf-8"),
    "/story": ("index.html", "text/html; charset=utf-8"),
    "/connect.js": ("connect.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/lib.js": ("lib.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class Job:
    def __init__(self) -> None:
        self.id = secrets.token_urlsafe(18)
        self.status = "queued"
        self.lines: list[str] = []
        self.error: str | None = None
        self.result: bytes | None = None
        self.finished: float | None = None
        self.lock = threading.Lock()

    def progress(self, message: str) -> None:
        with self.lock:
            self.lines.append(message)

    def finish(self, status: str, result: bytes | None = None, error: str | None = None) -> None:
        with self.lock:
            self.status, self.result, self.error = status, result, error
            self.finished = time.time()

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "lines": self.lines[since:],
                "next": len(self.lines),
                "error": self.error,
            }


class InputError(Exception):
    pass


def text_field(payload: dict[str, Any], key: str, limit: int) -> str:
    value = payload.get(key) or ""
    if not isinstance(value, str) or len(value) > limit:
        raise InputError(f"{key} is not valid")
    return value.strip()


def list_field(payload: dict[str, Any], key: str, limit: int, item_limit: int) -> list[str]:
    value = payload.get(key) or []
    if isinstance(value, str):
        value = re.split(r"[\s,]+", value)
    if not isinstance(value, list) or len(value) > limit:
        raise InputError(f"{key}: at most {limit} entries")
    items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if any(len(v) > item_limit for v in items):
        raise InputError(f"{key} has an entry that is too long")
    return items


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
PRETTY_HOST = "ghis.localhost"


class GhisServer:
    """Hosted mode serves strangers: an allowlist, https-only clones, temporary clones.
    Personal mode (bound to loopback, so only this computer can connect) also reads local
    folders, clones over ssh, keeps the clone cache and saves each story to out/."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.personal = args.host in LOOPBACK_HOSTS
        raw = args.allowed_hosts or os.environ.get("GHIS_ALLOWED_HOSTS")
        if not raw:
            raw = "*" if self.personal else DEFAULT_ALLOWED_HOSTS
        self.allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()
        self.slots = threading.Semaphore(args.max_jobs)
        self.ttl = (args.ttl or (24 * 60 if self.personal else 60)) * 60
        self.max_stored = args.max_stories
        self.git_jobs = args.jobs
        self.workdir = Path(args.workdir or tempfile.gettempdir()) / "ghis-jobs"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.identity_cache: tuple[float, dict[str, Any]] | None = None
        threading.Thread(target=self.reap, daemon=True).start()

    def home_repos(self) -> list[dict[str, Any]]:
        return discover_cached([str(Path.home())], HOME_DEPTH)

    def identity(self) -> dict[str, Any]:
        if self.identity_cache and time.time() - self.identity_cache[0] < 300:
            return self.identity_cache[1]
        found = detect_identity(self.home_repos())
        self.identity_cache = (time.time(), found)
        return found

    def detect(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What this computer already knows, so the form can be submitted untouched.
        Personal mode only; the token itself never leaves the server."""
        provider = text_field(payload, "provider", 20)
        if provider not in PROVIDERS:
            raise InputError("Unknown provider")
        repos = self.home_repos()
        result: dict[str, Any] = {
            "repos": len(repos),
            "identity": self.identity(),
            "url": None,
            "host": None,
            "token": None,
        }
        if provider in COLLECTORS:
            url = (text_field(payload, "url", 300) or PROVIDERS[provider]["url"]).rstrip("/")
            if url == PROVIDERS[provider]["url"]:
                url = suggest_url(provider, repos) or url
            host = urllib.parse.urlparse(url).hostname
            result.update(
                url=url, host=host, repos=sum(1 for r in repos if same_host(r["host"], host))
            )
            found = find_token(provider, url)
            if found:
                result["token"] = {"source": found[1], "username": found[2]}
        return result

    def config(self) -> dict[str, Any]:
        return {
            "providers": [
                {"id": k, "label": v["label"], "url": v["url"]} for k, v in PROVIDERS.items()
            ],
            "allowedHosts": sorted(self.allowed),
            "ttlMinutes": self.ttl // 60,
            "personal": self.personal,
            "outDir": tilde(DATA_DIR / "out") if self.personal else None,
        }

    def add(self, job: Job) -> Job:
        with self.jobs_lock:
            if len(self.jobs) >= self.max_stored:
                raise InputError("This server is busy right now. Try again in a few minutes.")
            self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self.jobs_lock:
            return self.jobs.get(job_id)

    def start(self, payload: dict[str, Any]) -> Job:
        provider = text_field(payload, "provider", 20)
        if provider not in PROVIDERS:
            raise InputError("Unknown provider")
        options = argparse.Namespace(
            url=text_field(payload, "url", 300) or None,
            email=list_field(payload, "emails", 10, 254),
            exclude_email=list_field(payload, "excludeEmails", 50, 254),
            repo=list_field(payload, "repos", 50, 500),
            name=text_field(payload, "name", 100) or None,
            clone_user=text_field(payload, "cloneUser", 100) or "git",
            local_root=list_field(payload, "folders", 20, 1000) if self.personal else [],
            depth=HOME_DEPTH,
            no_clone=False,
            clone_via="https",
            only_host=None,
            all_member_projects=False,
            count_all_files=False,
            jobs=self.git_jobs,
            insecure=False,
            cache="",
            allowed_hosts=None if "*" in self.allowed else self.allowed,
        )
        token = text_field(payload, "token", 500) or None
        job = self.add(Job())
        threading.Thread(target=self.run, args=(job, provider, options, token), daemon=True).start()
        return job

    def run(self, job: Job, provider: str, options: argparse.Namespace, token: str | None) -> None:
        if not self.slots.acquire(blocking=False):
            job.progress("Waiting for a free worker...")
            self.slots.acquire()
        # Personal mode keeps clones between runs so the next story only fetches.
        cache = (
            DATA_DIR / "cache"
            if self.personal
            else Path(tempfile.mkdtemp(prefix="job-", dir=self.workdir))
        )
        options.cache = str(cache)
        _sink.job = job
        job.status = "running"
        notes: list[str] = []
        requested = provider
        try:
            if self.personal:
                provider, token = self.fill_in(job, provider, options, token, notes)
            data = collect(options, provider, token)
            data["warnings"] = notes + data["warnings"]
            if provider != requested:
                # Built without the API, but it is still this host's story: keep its name and
                # vocabulary (merge vs pull requests) and record that no API data is in it.
                data["source"].update(
                    provider=requested,
                    label=PROVIDERS[requested]["label"],
                    host=options.only_host,
                    has=[],
                )
            if self.personal:
                write_site(data, DATA_DIR / "out", False)  # logs where the copy went
            job.finish("done", json.dumps(data, separators=(",", ":")).encode())
        except CollectError as err:
            job.finish("failed", error=str(err))
        except Exception as err:  # report, never leak a traceback (or a token) to the client
            print(
                f"[ghis] job {job.id[:6]} crashed: {type(err).__name__}: {err}",
                file=sys.stderr,
                flush=True,
            )
            job.finish(
                "failed",
                error="Something went wrong while collecting. Try again, "
                "or run the CLI locally for details.",
            )
        finally:
            _sink.job = None
            token = None
            if not self.personal:
                shutil.rmtree(cache, ignore_errors=True)
            self.slots.release()

    def fill_in(
        self,
        job: Job,
        provider: str,
        options: argparse.Namespace,
        token: str | None,
        notes: list[str],
    ) -> tuple[str, str | None]:
        """Personal mode: whatever the form left empty, take from this computer."""
        if not options.local_root:
            job.progress("Looking for repositories in your home folder...")
            options.local_root = [str(Path.home())]
        # The emails found on this computer, minus the ones unticked, plus any typed in.
        excluded = set(options.exclude_email)
        detected = [e["email"] for e in self.identity()["emails"] if e["email"] not in excluded]
        options.email = sorted(set(options.email) | set(detected))
        if provider in COLLECTORS and not options.url:
            # Same guess the detection panel shows: the instance your checkouts point at.
            options.url = suggest_url(provider, self.home_repos())
        if provider in COLLECTORS and not token:
            label = PROVIDERS[provider]["label"]
            url = (options.url or PROVIDERS[provider]["url"]).rstrip("/")
            found = find_token(provider, url)
            if found:
                token = found[0]
                job.progress(f"Using your {label} token from {found[1]} (@{found[2]}).")
            else:
                host = urllib.parse.urlparse(url).hostname
                notes.append(
                    f"No {label} token was found on this computer, so this story is "
                    f"built from your local checkouts of {host} repositories only. Add a "
                    "token to include pull or merge requests, reviews and repositories "
                    "you never cloned."
                )
                job.progress(f"No {label} token found: using your local {host} repositories.")
                options.only_host = host
                provider = "git"
        return provider, token

    def upload(self, raw: bytes) -> Job:
        try:
            data = json.loads(raw)
        except ValueError:
            raise InputError("That file is not JSON") from None
        if (
            not isinstance(data, dict)
            or data.get("schema") != SCHEMA_VERSION
            or not isinstance(data.get("commits"), list)
        ):
            raise InputError("That file is not a ghis history.json")
        job = self.add(Job())
        job.finish("done", json.dumps(data, separators=(",", ":")).encode())
        return job

    def reap(self) -> None:
        while True:
            time.sleep(60)
            cutoff = time.time() - self.ttl
            with self.jobs_lock:
                for job_id in [
                    j.id for j in self.jobs.values() if j.finished and j.finished < cutoff
                ]:
                    del self.jobs[job_id]


def make_handler(app: GhisServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ghis"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            # Job ids are bearer secrets for their stories; keep them out of access logs.
            line = fmt % args
            print(
                f"[ghis] {re.sub(r'/api/jobs/[^/ ?]+', '/api/jobs/…', line)}",
                file=sys.stderr,
                flush=True,
            )

        def send(
            self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status: int, payload: Any) -> None:
            self.send(
                status,
                json.dumps(payload).encode(),
                "application/json",
                {"Cache-Control": "no-store"},
            )

        def body(self, limit: int) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise InputError(f"Request too large (limit {limit // 1_000_000} MB)")
            return self.rfile.read(length)

        def host_ok(self) -> bool:
            # A personal server reads local folders, so it must only answer to names that
            # mean this computer: a web page using DNS rebinding arrives with its own name.
            # *.localhost (ghis.localhost) qualifies: browsers resolve it to loopback
            # themselves (RFC 6761), so no DNS answer can point it anywhere else.
            if not app.personal:
                return True
            host = (self.headers.get("Host") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return name in ("localhost", "127.0.0.1", "[::1]") or name.endswith(".localhost")

        def same_origin(self) -> bool:
            # Browsers send Origin on cross-site POSTs; a page elsewhere may not start jobs.
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return (
                urllib.parse.urlparse(origin).netloc.lower()
                == (self.headers.get("Host") or "").lower()
            )

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self.host_ok():
                self.json(403, {"error": "Unexpected Host header"})
                return
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path in STATIC:
                name, content_type = STATIC[path]
                body = (WEB_DIR / name).read_bytes()
                if content_type.startswith("text/html"):
                    body = version_refs(body.decode("utf-8"), WEB_DIR).encode("utf-8")
                self.send(200, body, content_type, {"Cache-Control": "no-cache"})
            elif path == "/data.js":  # the offline page's data file; empty when hosted
                self.send(200, b"", "text/javascript; charset=utf-8")
            elif path == "/healthz":
                self.json(200, {"ok": True})
            elif path == "/api/config":
                self.json(200, app.config())
            elif path == "/api/demo":
                provider = urllib.parse.parse_qs(url.query).get("provider", ["github"])[0]
                provider = provider if provider in COLLECTORS else "github"
                self.json(200, make_demo(7, provider))
            elif match := re.fullmatch(r"/api/jobs/([\w-]{10,40})(/result)?", path):
                job = app.get(match.group(1))
                if not job:
                    self.json(404, {"error": "This story has expired or never existed."})
                elif match.group(2):
                    if job.result is None:
                        self.json(409, {"error": "Not ready yet"})
                        return
                    download = "download" in urllib.parse.parse_qs(url.query)
                    extra = {"Cache-Control": "no-store"}
                    if download:
                        extra["Content-Disposition"] = 'attachment; filename="history.json"'
                    self.send(200, job.result, "application/json", extra)
                else:
                    since = int(urllib.parse.parse_qs(url.query).get("since", ["0"])[0] or 0)
                    self.json(200, job.snapshot(max(0, since)))
            else:
                self.json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if not self.host_ok() or not self.same_origin():
                self.json(403, {"error": "Cross-site requests are not allowed"})
                return
            if content_type != "application/json":
                # Forces a CORS preflight for any cross-site caller, which this server never grants.
                self.json(415, {"error": "Send application/json"})
                return
            try:
                if path == "/api/jobs":
                    payload = json.loads(self.body(64_000) or b"{}")
                    if not isinstance(payload, dict):
                        raise InputError("Expected a JSON object")
                    self.json(202, {"id": app.start(payload).id})
                elif path == "/api/detect" and app.personal:
                    payload = json.loads(self.body(64_000) or b"{}")
                    if not isinstance(payload, dict):
                        raise InputError("Expected a JSON object")
                    self.json(200, app.detect(payload))
                elif path == "/api/upload":
                    self.json(201, {"id": app.upload(self.body(64_000_000)).id})
                else:
                    self.json(404, {"error": "Not found"})
            except InputError as err:
                self.json(400, {"error": str(err)})
            except ValueError:
                self.json(400, {"error": "Malformed request"})

    return Handler


def serve(args: argparse.Namespace) -> None:
    app = GhisServer(args)
    if not app.personal:
        gitmod.GIT_TIMEOUT = args.git_timeout
    # Without an explicit port, a personal server steps past ports other apps already use
    # (8080 is a popular default); an explicit --port or PORT is honoured exactly.
    explicit = args.port is not None
    first = args.port if explicit else 8080
    candidates = [first] if explicit or not app.personal else range(first, first + 20)
    httpd = None
    for port in candidates:
        try:
            httpd = ThreadingHTTPServer((args.host, port), make_handler(app))
            break
        except OSError as err:
            error = err
    if httpd is None:
        hint = "Pick another with --port." if explicit else "Pick a free one with --port."
        die(f"Port {first} is already in use by another program ({error.strerror}). {hint}")
    port = httpd.server_address[1]
    if port != first:
        log(f"Port {first} is busy (another program is using it), so ghis is using {port}.")
    httpd.daemon_threads = True
    url = f"http://{args.host}:{port}/"
    if app.personal:
        # ghis.localhost needs no setup: Chrome, Firefox and Edge send every *.localhost name
        # to this computer. Safari leaves it to macOS, which doesn't, so on a Mac the browser
        # opens plain localhost.
        url = f"http://{'localhost' if sys.platform == 'darwin' else PRETTY_HOST}:{port}/"
        log(f"ghis is running at {url}  (also http://localhost:{port}/)")
    else:
        log(f"ghis is serving on {url}  (allowed hosts: {', '.join(sorted(app.allowed))})")
    log("Press Ctrl+C to stop.")
    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Stopped.")
