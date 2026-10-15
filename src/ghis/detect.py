"""Personal mode's detective work, so the form can be submitted untouched: find
checkouts, the emails you commit with, your instance and a token you already have.
Nothing found here leaves the computer except to the host it belongs to."""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .api import ApiError, Http
from .git import GitError, discover_local, git
from .providers import PROVIDERS
from .providers.github import github_api_urls

_discovered: dict[tuple[tuple[str, ...], int], tuple[float, list[dict[str, Any]]]] = {}


_discover_lock = threading.Lock()


PROVIDER_HINTS = {
    "github": ("github",),
    "gitlab": ("gitlab",),
    "gitea": ("codeberg", "gitea", "forgejo"),
}


def discover_cached(roots: list[str], depth: int, max_age: float = 300) -> list[dict[str, Any]]:
    """discover_local, remembered for a few minutes: walking a home folder takes seconds."""
    key = (tuple(roots), depth)
    with _discover_lock:
        hit = _discovered.get(key)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    found = discover_local(roots, depth)
    with _discover_lock:
        _discovered[key] = (time.time(), found)
    return found


def global_git(key: str) -> str:
    try:
        return git(["config", "--global", key]).strip()
    except GitError:
        return ""


def detect_identity(repos: list[dict[str, Any]]) -> dict[str, Any]:
    """The emails you commit with: every author email paired with your git user.name,
    plus user.email itself. Catches work, personal and noreply addresses alike."""
    name = global_git("user.name")
    email = global_git("user.email").lower()
    counts: dict[str, int] = {}

    def authors(repo: dict[str, Any]) -> str:
        try:
            return git(
                ["-C", repo["dir"], "log", "--all", "--exclude=refs/stash", "--format=%an%x1f%ae"]
            )
        except GitError:
            return ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for out in pool.map(authors, repos):
            for line in out.splitlines():
                author, _, address = line.partition("\x1f")
                address = address.strip().lower()
                if address and (
                    (name and author.strip().lower() == name.lower()) or address == email
                ):
                    counts[address] = counts.get(address, 0) + 1
    if email and email not in counts:
        counts[email] = 0
    return {
        "name": name,
        "emails": [
            {"email": e, "commits": n} for e, n in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    }


def suggest_url(provider: str, repos: list[dict[str, Any]]) -> str | None:
    """The instance your checkouts point at, e.g. a company GitLab, if its name says so."""
    counts: dict[str, int] = {}
    for repo in repos:
        host = (repo.get("host") or "").lower()
        if any(hint in host for hint in PROVIDER_HINTS.get(provider, ())):
            counts[host] = counts.get(host, 0) + 1
    if not counts:
        return None
    host = max(counts, key=lambda h: counts[h])
    return "https://github.com" if host in ("github.com", "ssh.github.com") else f"https://{host}"


def run_quiet(cmd: list[str], stdin: str | None = None) -> str:
    """Run a helper without ever letting it prompt; an empty string on any failure."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GH_PROMPT_DISABLED": "1",
    }
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            env=env,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def token_candidates(provider: str, url: str) -> list[tuple[str, str]]:
    """Tokens this computer already has for the host, with where each came from."""
    host = urllib.parse.urlparse(url).hostname or ""
    found: list[tuple[str, str]] = []
    env_name = PROVIDERS[provider]["token_env"]
    if os.environ.get(env_name):
        found.append((os.environ[env_name], f"${env_name}"))
    tool = {
        "github": ["gh", "auth", "token", "--hostname", host],
        "gitlab": ["glab", "config", "get", "token", "--host", host],
    }.get(provider)
    if tool and shutil.which(tool[0]):
        token = run_quiet(tool).strip()
        if token:
            found.append((token, f"the {tool[0]} CLI"))
    credential = run_quiet(
        ["git", "-c", "credential.interactive=never", "credential", "fill"],
        f"protocol=https\nhost={host}\n\n",
    )
    for line in credential.splitlines():
        if line.startswith("password="):
            found.append((line[len("password=") :], "git's credential helper"))
    return found


def whoami(provider: str, url: str, token: str) -> str | None:
    """The username a token belongs to, or None when the host turns it down."""
    try:
        if provider == "github":
            http = Http(github_api_urls(url)[0], {"Authorization": f"Bearer {token}"}, False, 8)
            return http.request("/user")[0]["login"]
        if provider == "gitlab":
            return Http(url + "/api/v4", {"PRIVATE-TOKEN": token}, False, 8).request("/user")[0][
                "username"
            ]
        if provider == "gitea":
            return Http(url + "/api/v1", {"Authorization": f"token {token}"}, False, 8).request(
                "/user"
            )[0]["login"]
    except (ApiError, KeyError, TypeError, ValueError):
        return None
    return None


def find_token(provider: str, url: str) -> tuple[str, str, str] | None:
    """(token, source, username) for the first token on this computer the host accepts."""
    for token, source in token_candidates(provider, url):
        username = whoami(provider, url, token)
        if username:
            return token, source, username
    return None
