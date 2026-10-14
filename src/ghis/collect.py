"""Gathers a user's history from an API and/or git into the history.json shape."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import ApiError
from .common import SCHEMA_VERSION, die, host_allowed, log, same_host
from .detect import discover_cached
from .git import (
    DEFAULT_IGNORE,
    GitError,
    check_git,
    clone_env,
    clone_url,
    git,
    parse_remote,
    scan_repo,
    sync_clone,
)
from .providers import COLLECTORS, PROVIDERS


def pick_provider(args: argparse.Namespace) -> str:
    if args.provider:
        return args.provider
    for name, spec in PROVIDERS.items():
        if name != "git" and os.environ.get(spec["token_env"]):
            return name
    return "git"


def read_token(args: argparse.Namespace, provider: str) -> str | None:
    if args.token_file:
        return Path(args.token_file).expanduser().read_text().strip() or None
    return os.environ.get(PROVIDERS[provider]["token_env"]) or os.environ.get("GHIS_TOKEN")


def project_from_url(url: str) -> dict[str, Any]:
    host, path = parse_remote(url)
    if not path:
        die(f"Not a clone URL: {url}")
    web = f"https://{host}/{path}" if host else None
    return {
        "path": path,
        "name": path.split("/")[-1],
        "webUrl": web,
        "cloneUrl": url,
        "source": "url",
    }


def collect(args: argparse.Namespace, provider: str, token: str | None) -> dict[str, Any]:
    """Gather everything into the history.json shape. `args.allowed_hosts` (a set) is how
    the server confines a request to hosts it trusts; the CLI leaves it None."""
    check_git()
    allowed: set[str] | None = getattr(args, "allowed_hosts", None)
    restricted = allowed is not None
    uses_api = provider in COLLECTORS
    if uses_api and not token:
        die(
            f"No token for {PROVIDERS[provider]['label']}. Set "
            f"{PROVIDERS[provider]['token_env']} or pass --token-file; README.md lists the "
            "scopes it needs."
        )
    if not uses_api and not args.local_root and not args.repo:
        die(
            "Nothing to read from. Pass --repo URL (any host) and/or --local-root DIR, or "
            "pick an API provider with a token."
        )
    base_url = (args.url or PROVIDERS[provider]["url"]).rstrip("/")
    if uses_api:
        if urllib.parse.urlparse(base_url).scheme not in ("http", "https"):
            die(f"Instance URL must start with https:// ({base_url})")
        if not host_allowed(urllib.parse.urlparse(base_url).hostname, allowed):
            die(f"This server is not allowed to contact {base_url}.")

    warnings: list[str] = []
    emails = {e.strip().lower() for e in args.email if e.strip()}
    user: dict[str, Any] = {}
    projects: dict[str, dict[str, Any]] = {}
    api: dict[str, Any] = {
        "requests": [],
        "reviews": [],
        "activity": {},
        "has": [],
        "cloneUser": args.clone_user,
    }

    if uses_api:
        try:
            api = COLLECTORS[provider](base_url, token, args, warnings)
        except ApiError as err:
            die(f"{PROVIDERS[provider]['label']} API failed: {err}")
        user = api["user"]
        emails |= api["emails"]
        for project in api["projects"]:
            projects[project["path"].lower()] = project

    for url in args.repo:
        url = url.strip()
        if not url:
            continue
        project = project_from_url(url)
        if restricted:
            host = parse_remote(url)[0]
            if not url.startswith("https://") or not host_allowed(host, allowed):
                die(f"This server only clones https:// URLs on allowed hosts ({url}).")
        projects.setdefault(project["path"].lower(), project)["cloneUrl"] = url

    local = []
    if args.local_root:
        log("Looking for local checkouts...")
        local = discover_cached(args.local_root, args.depth)
        log(f"  {len(local)} repositories")
        # A story for one host only uses checkouts of that host: a GitLab story must not
        # quietly include the GitHub repositories that happen to sit in the same folder.
        only_host = getattr(args, "only_host", None) or (
            urllib.parse.urlparse(base_url).hostname if uses_api else None
        )
        if only_host:
            local = [r for r in local if same_host(r["host"], only_host)]
            log(f"  {len(local)} of them are on {only_host}")
            if not local and not uses_api and not args.repo:
                die(
                    f"None of your local repositories come from {only_host}, and there is no "
                    "token to fetch them with. Add a token, or pick another provider."
                )

    if not emails and not restricted:
        try:
            emails.add(git(["config", "--global", "user.email"]).strip().lower())
        except GitError:
            pass
    emails.discard("")
    if not emails:
        die("No author email to match commits against; add the email(s) you commit with.")
    if not user.get("name"):
        user["name"] = args.name or ""
    if not user.get("name") and not restricted:
        try:
            user["name"] = git(["config", "--global", "user.name"]).strip()
        except GitError:
            pass
    user["name"] = user.get("name") or sorted(emails)[0].split("@")[0]
    log(f"Matching commits by: {', '.join(sorted(emails))}")

    targets: list[tuple[str, str]] = []
    for repo in local:
        key = repo["path"].lower() if repo["path"] else f"local:{repo['dir']}"
        if any(key == k for k, _ in targets):
            continue
        if key in projects:
            projects[key]["source"] = "api+local"
        else:
            host = repo["host"]
            projects[key] = {
                "path": repo["path"] or Path(repo["dir"]).name,
                "name": (repo["path"] or repo["dir"]).rstrip("/").split("/")[-1],
                "webUrl": f"https://{host}/{repo['path']}" if host and repo["path"] else None,
                "source": "local",
            }
        targets.append((key, repo["dir"]))

    cloned = 0
    if not args.no_clone:
        local_keys = {k for k, _ in targets}
        to_clone = [
            (k, p)
            for k, p in projects.items()
            if k not in local_keys and clone_url(p, args.clone_via)
        ]
        if restricted:
            refused = [
                p["path"]
                for _, p in to_clone
                if not host_allowed(parse_remote(clone_url(p, args.clone_via) or "")[0], allowed)
            ]
            if refused:
                warnings.append(
                    f"Skipped {len(refused)} repositories on hosts this server "
                    f"may not contact: {', '.join(refused[:5])}"
                )
            to_clone = [(k, p) for k, p in to_clone if p["path"] not in refused]
        if to_clone:
            cache = Path(args.cache).expanduser().resolve()
            cache.mkdir(parents=True, exist_ok=True)
            env = clone_env(token, api["cloneUser"] or "git", restricted)
            log(f"Cloning/fetching {len(to_clone)} repositories into {cache} ...")

            def sync(item: tuple[str, dict[str, Any]]) -> tuple[str, str | None, str | None]:
                key, project = item
                try:
                    return key, str(sync_clone(project, cache, args.clone_via, env)), None
                except GitError as err:
                    return key, None, str(err)

            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
                for done, (key, path, error) in enumerate(pool.map(sync, to_clone), 1):
                    name = projects[key]["path"]
                    if path:
                        targets.append((key, path))
                        cloned += 1
                        log(f"  [{done}/{len(to_clone)}] {name}")
                    else:
                        warnings.append(f"Could not clone {name}: {error}")
                        log(f"  [{done}/{len(to_clone)}] {name} FAILED: {error}")

    log(f"Reading commits from {len(targets)} repositories...")
    ignore = () if args.count_all_files else DEFAULT_IGNORE
    author_emails = sorted(emails)
    seen: set[str] = set()
    per_project: dict[str, list[dict[str, Any]]] = {}

    def scan(item: tuple[str, str]) -> tuple[str, list[dict[str, Any]], str | None]:
        key, path = item
        try:
            return key, scan_repo(path, author_emails, ignore), None
        except GitError as err:
            return key, [], str(err)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for key, found, error in pool.map(scan, targets):
            if error:
                warnings.append(f"Could not read {projects[key]['path']}: {error}")
                continue
            unique = [c for c in found if c["h"] not in seen]
            seen.update(c["h"] for c in unique)
            per_project.setdefault(key, []).extend(unique)

    request_paths = {r["project"].lower() for r in api["requests"] + api["reviews"]}
    kept = [k for k in projects if per_project.get(k) or k in request_paths]
    index = {key: i for i, key in enumerate(kept)}
    commits = []
    for key, found in per_project.items():
        if key in index:
            commits.extend({**c, "h": c["h"][:12], "r": index[key]} for c in found)
    commits.sort(key=lambda c: datetime.fromisoformat(c["t"]))
    log(f"  {len(commits)} commits across {sum(1 for k in kept if per_project.get(k))} repos")

    data = {
        "schema": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "provider": provider,
            "label": PROVIDERS[provider]["label"],
            "host": urllib.parse.urlparse(base_url).hostname if uses_api else None,
            "has": api["has"],
            "localRepos": len(local),
            "clonedRepos": cloned,
            "emails": author_emails,
            "linesIgnored": list(ignore),
        },
        "user": user,
        "projects": [
            {k: v for k, v in projects[key].items() if k not in ("sshUrl", "httpUrl")}
            for key in kept
        ],
        "commits": commits,
        "requests": api["requests"],
        "reviews": api["reviews"],
        "activity": api["activity"],
        "warnings": warnings,
    }
    for warning in warnings:
        log(f"warning: {warning}")
    return data
