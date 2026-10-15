"""Gathers a user's history from an API and/or git into the history.json shape."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    last_fetch,
    parse_remote,
    remove_tree,
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
        return Path(args.token_file).expanduser().read_text(encoding="utf-8").strip() or None
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


CLONES_PREFIX = "ghis-clones-"


def collect(args: argparse.Namespace, provider: str, token: str | None) -> dict[str, Any]:
    """Gather everything into the history.json shape. `args.allowed_hosts` (a set) is how
    the server confines a request to hosts it trusts; the CLI leaves it None.

    Clones go to a temporary folder that is deleted afterwards, whatever happens, unless
    `args.cache` names a folder to keep them in (--keep-clones), where the next run only
    fetches what changed."""
    if args.cache or args.no_clone:
        return _collect(args, provider, token)
    base = getattr(args, "workdir", None) or tempfile.gettempdir()
    sweep_stale_clones(Path(base))
    clones = Path(tempfile.mkdtemp(prefix=CLONES_PREFIX, dir=base))
    try:
        return _collect(argparse.Namespace(**{**vars(args), "cache": str(clones)}), provider, token)
    finally:
        try:
            remove_tree(clones)
        except OSError:
            pass  # a file still locked (Windows); the next run sweeps it


def sweep_stale_clones(base: Path, age: float = 24 * 3600) -> None:
    """Delete clone folders a killed run left behind. Only day-old ones: a younger folder
    may belong to a run that is still going."""
    for leftover in base.glob(f"{CLONES_PREFIX}*"):
        try:
            if leftover.is_dir() and time.time() - leftover.stat().st_mtime > age:
                remove_tree(leftover)
        except OSError:
            pass


def _collect(args: argparse.Namespace, provider: str, token: str | None) -> dict[str, Any]:
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
    # Addresses the user unticked (a teammate who shares their git name, an old shared
    # machine) stay out, wherever they came from: detected, typed or the API.
    excluded = {e.strip().lower() for e in getattr(args, "exclude_email", None) or [] if e.strip()}
    emails -= excluded
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
    local_keys = {k for k, _ in targets}
    skipped: list[dict[str, str]] = []

    to_clone: list[tuple[str, dict[str, Any]]] = []
    if not args.no_clone:
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
            skipped.extend(
                {"path": p, "reason": "on a host this server may not contact"} for p in refused
            )
            if refused:
                warnings.append(
                    f"Skipped {len(refused)} repositories on hosts this server "
                    f"may not contact: {', '.join(refused[:5])}"
                )
            to_clone = [(k, p) for k, p in to_clone if p["path"] not in refused]

    ignore = () if args.count_all_files else DEFAULT_IGNORE
    author_emails = sorted(emails)
    env = clone_env(token, api["cloneUser"] or "git", restricted)
    Found = tuple[list[dict[str, Any]], Optional[str]]

    def scan(path: str, fetch_env: dict[str, str] | None) -> Found:
        try:
            return scan_repo(path, author_emails, ignore, fetch_env), None
        except GitError as err:
            return [], str(err)

    def sync(project: dict[str, Any]) -> tuple[str | None, str]:
        try:
            path, how = sync_clone(project, cache, args.clone_via, env, author_emails)
            return str(path), how
        except GitError as err:
            return None, str(err)

    # Each repository is read as soon as it is on disk, while the others still download.
    # Downloads mostly wait on the network, so twice as many run at once as reads.
    reads: dict[str, concurrent.futures.Future[Found]] = {}
    clones: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as readers:
        for key, path in targets:  # local checkouts: nothing to download
            reads[key] = readers.submit(scan, path, None)
        if to_clone:
            cache = Path(args.cache).expanduser().resolve()
            cache.mkdir(parents=True, exist_ok=True)
            log(
                f"Downloading {len(to_clone)} repositories: only what your commits need, "
                "read as each one arrives..."
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs * 2) as downloads:
                started = {downloads.submit(sync, p): k for k, p in to_clone}
                for done, future in enumerate(concurrent.futures.as_completed(started), 1):
                    key = started[future]
                    name = projects[key]["path"]
                    path, how = future.result()
                    if path:
                        clones[key] = path
                        # The token too: anything still missing is fetched while reading.
                        reads[key] = readers.submit(scan, path, env)
                        log(f"  [{done}/{len(to_clone)}] {name} ({how})")
                    else:
                        warnings.append(f"Could not clone {name}: {how}")
                        skipped.append({"path": name, "reason": f"could not be cloned: {how}"})
                        log(f"  [{done}/{len(to_clone)}] {name} FAILED: {how}")
        log(f"Reading commits from {len(reads)} repositories...")
    cloned = len(clones)
    # Local checkouts first, then clones in the order the host listed them: the same order
    # every run, so a change found in two repositories is always credited to the same one.
    targets += [(k, clones[k]) for k, _ in to_clone if k in clones]

    # One change can carry several ids: a fork shares it, but a rebase or a cherry-pick copies
    # it under a new one. Copies keep the author, the author time and the message, so those
    # identify a change alongside its id.
    seen: set[str] = set()
    per_project: dict[str, list[dict[str, Any]]] = {}
    for key, _ in targets:
        found, error = reads[key].result()
        if error:
            warnings.append(f"Could not read {projects[key]['path']}: {error}")
            skipped.append({"path": projects[key]["path"], "reason": f"could not be read: {error}"})
            continue
        unique = []
        for c in found:
            keys = (c["h"], f"{c['t']}\x1f{c['e']}\x1f{c['s']}")
            if not any(k in seen for k in keys):
                seen.update(keys)
                unique.append(c)
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

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # What the story covers, for the page's "What this story covers" section: how each
    # repository was read and how current it is, what was skipped, what the host lacks.
    dirs = dict(targets)
    coverage_repos = []
    for key in kept:
        if key not in dirs:
            via, fetched = "api", None  # only pull/merge requests or reviews
        elif key in local_keys:
            via, fetched = "local", last_fetch(dirs[key])
        else:
            via, fetched = "clone", generated
        coverage_repos.append(
            {
                "path": projects[key]["path"],
                "via": via,
                "commits": len(per_project.get(key, [])),
                "fetchedAt": fetched,
            }
        )
    limits = []
    if not uses_api:
        limits.append(
            "No hosting API was used, so pull or merge requests, reviews and comments "
            "are not included, nor repositories you never cloned."
        )
    elif provider == "github":
        limits.append(
            "GitHub's API does not expose your comment history, so comments are not counted."
        )
    elif provider == "gitea":
        limits.append(
            "Codeberg, Forgejo and Gitea do not list who reviewed your pull requests, "
            "so People shows only whom you reviewed."
        )
    elif provider == "gitlab" and api["activity"]:
        oldest = min(api["activity"])[:10]
        limits.append(
            f"GitLab keeps activity events since {oldest}: reviews and comments before "
            "it are not counted. Commits are unaffected."
        )

    data = {
        "schema": SCHEMA_VERSION,
        "generatedAt": generated,
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
        "coverage": {
            "repos": coverage_repos,
            "scanned": len(targets),
            "skipped": skipped,
            "limits": limits,
            "excludedEmails": sorted(excluded),
        },
        "warnings": warnings,
    }
    for warning in warnings:
        log(f"warning: {warning}")
    return data
