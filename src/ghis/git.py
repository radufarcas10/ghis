"""Everything that runs git: finding checkouts, cloning and reading commits."""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import die, log

# Files whose line counts say nothing about the work done: lockfiles, build output,
# vendored code and test snapshots. Matched against the full path with fnmatch, where
# `*` also crosses `/`. Their commits still count; only their lines are dropped.
DEFAULT_IGNORE = (
    "*package-lock.json",
    "*yarn.lock",
    "*pnpm-lock.yaml",
    "*composer.lock",
    "*Gemfile.lock",
    "*poetry.lock",
    "*Pipfile.lock",
    "*Cargo.lock",
    "*go.sum",
    "*.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.snap",
    "*__snapshots__/*",
    "dist/*",
    "*/dist/*",
    "vendor/*",
    "*/vendor/*",
    "node_modules/*",
    "*/node_modules/*",
    ".nuxt/*",
    ".output/*",
)


LANGUAGES = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".py": "Python",
    ".php": "PHP",
    ".rb": "Ruby",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".cs": "C#",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".swift": "Swift",
    ".scss": "SCSS",
    ".sass": "SCSS",
    ".css": "CSS",
    ".less": "Less",
    ".html": "HTML",
    ".htm": "HTML",
    ".twig": "Twig",
    ".md": "Markdown",
    ".mdx": "Markdown",
    ".rst": "reStructuredText",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".xml": "XML",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".sql": "SQL",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".tf": "Terraform",
    ".feature": "Gherkin",
    ".dart": "Dart",
    ".lua": "Lua",
}


FILENAMES = {"makefile": "Makefile", "jenkinsfile": "Groovy"}


# Directories never descended into when looking for local checkouts.
SKIP_DIRS = {
    "node_modules",
    "vendor",
    "dist",
    "venv",
    "__pycache__",
    "cache",
    "snap",
    "Library",
    "AppData",
    "Applications",
    "Pictures",
    "Music",
    "Videos",
    "Movies",
}


HOME_DEPTH = 6  # how deep a personal server looks for checkouts under the home folder


LOG_FORMAT = "%x1e%H%x1f%P%x1f%aI%x1f%ae%x1f%s"
# The same, plus the Co-authored-by trailers (\x1d-separated) before the subject.
CO_FORMAT = (
    "%x1e%H%x1f%P%x1f%aI%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1d)%x1f%s"
)


RENAME_BRACES = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


class GitError(Exception):
    pass


# Seconds any single git command may run; a hosted server sets it, the CLI leaves it off.
GIT_TIMEOUT: float | None = None


MIN_GIT = (2, 31)  # GIT_CONFIG_COUNT, used to pass the clone token, arrived in 2.31


def git(args: list[str], env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git took longer than {GIT_TIMEOUT:.0f}s and was stopped") from None
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise GitError(lines[-1] if lines else f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def parse_remote(url: str) -> tuple[str | None, str | None]:
    url = url.strip()
    if "://" in url:
        parsed = urllib.parse.urlparse(url)
        host, path = parsed.hostname, parsed.path
    else:
        match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", url)
        if not match:
            return None, None
        host, path = match.group(1), match.group(2)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return host, path or None


def discover_local(roots: list[str], max_depth: int) -> list[dict[str, Any]]:
    """Find git checkouts under the roots, one per repository (worktrees collapse)."""
    found: dict[str, dict[str, Any]] = {}
    for root in roots:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            log(f"  skipping {root_path}: not a directory")
            continue
        base_depth = len(root_path.parts)
        for dirpath, dirnames, filenames in os.walk(root_path):
            here = Path(dirpath)
            if ".git" in dirnames or ".git" in filenames:
                try:
                    common = str(
                        (
                            here / git(["-C", str(here), "rev-parse", "--git-common-dir"]).strip()
                        ).resolve()
                    )
                except GitError:
                    common = ""
                if common and common not in found:
                    try:
                        remote = git(["-C", str(here), "config", "--get", "remote.origin.url"])
                    except GitError:
                        remote = ""
                    host, path = parse_remote(remote) if remote else (None, None)
                    found[common] = {"dir": str(here), "host": host, "path": path}
            if len(here.parts) - base_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    return list(found.values())


def clone_env(token: str | None, clone_user: str, restricted: bool) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    config: list[tuple[str, str]] = []
    if token:
        # Passed through the environment so the token never lands in argv or a git config.
        # Only sent over http(s); ssh clones authenticate with your ssh keys as usual.
        credentials = base64.b64encode(f"{clone_user}:{token}".encode()).decode()
        config.append(("http.extraHeader", f"Authorization: Basic {credentials}"))
    if restricted:
        env["GIT_ALLOW_PROTOCOL"] = "https"
        config.append(("http.followRedirects", "false"))
    if config:
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def remove_tree(path: Path) -> None:
    """shutil.rmtree that also deletes read-only files: git marks its objects read-only on
    Windows, where a plain rmtree stops with "access denied"."""

    def retry(func: Any, target: str, *_: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


# Up to this share of a repository's history being yours (with the commit before each of
# yours), fetching only the snapshots your commits need beats downloading every folder
# listing at once. Each targeted request costs the host time to assemble, so it has to save
# a lot: measured on Django, 5% of the history halved the download (37 MB instead of 75)
# for some 10 s of extra server time, a win below ~5 MB/s. Above it, one bulk download wins.
TARGETED_SHARE = 0.10


def sync_clone(
    project: dict[str, Any],
    cache: Path,
    via: str,
    env: dict[str, str] | None,
    emails: list[str],
) -> tuple[Path, str]:
    """A bare copy of a repository holding everything needed to read your commits, and how
    it was made, for the progress line.

    The cheapest way that works, tried in order:
    1. "commits only" (--filter=tree:0): the history without folder listings or files, a few
       percent of a full clone; then, in one request each, the folder listings of the
       snapshots your commits compare, and the file versions they changed. Best when your
       commits are a small part of a long history.
    2. "history" (--filter=blob:none): every commit and folder listing, then only your files.
       Chosen when a large share of the history is yours.
    3. a full clone, for a host that can't serve either.
    numstat reads exactly the same data either way, so the numbers do not change."""
    dest = cache / (re.sub(r"[^\w.-]+", "__", project["path"]) + ".git")
    url = clone_url(project, via)
    if not url:
        raise GitError("no clone URL")
    if dest.exists():  # kept from an earlier run (--keep-clones): fetch what changed
        partial = is_partial(dest)
        if partial:
            git(["-C", str(dest), "remote", "set-url", "origin", url], env=env)
        git(
            [
                "-C",
                str(dest),
                "fetch",
                "--quiet",
                "--prune",
                "--force",
                # The promisor remote by name, so the fetch keeps the clone's filter.
                "origin" if partial else url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            env=env,
        )
        if partial:
            fetch_your_files(dest, emails, env)
        return dest, "updated"
    building = dest.with_suffix(".partial")
    failure: GitError | None = None
    for strategy in ("tree:0", "blob:none", ""):
        if building.exists():
            remove_tree(building)
        try:
            flags = [f"--filter={strategy}"] if strategy else []
            git(["clone", "--bare", "--quiet", *flags, url, str(building)], env=env)
            if not strategy or not is_partial(building):
                how = "full clone"  # asked for one, or the host ignored the filter
            elif strategy == "tree:0" and share_needed(building, emails) > TARGETED_SHARE:
                continue  # mostly your history: all folder listings at once is cheaper
            else:
                fetch_your_files(building, emails, env)
                how = "commits only" if strategy == "tree:0" else "history"
            building.rename(dest)
            return dest, how
        except GitError as err:
            failure = err  # e.g. a host that refuses this filter: try the next way
    if building.exists():
        remove_tree(building)
    raise failure or GitError("could not clone")


def is_partial(repo: Path) -> bool:
    """Whether a clone left objects on the server to fetch later. Recent git marks the
    remote (remote.origin.promisor); older versions set extensions.partialClone."""
    for key in ("remote.origin.promisor", "extensions.partialClone"):
        value = config_value(repo, key).lower()
        if value and value != "false":
            return True
    return False


def config_value(repo: Path, key: str) -> str:
    try:
        return git(["-C", str(repo), "config", "--get", key]).strip()
    except GitError:
        return ""  # not set


def your_commits(repo: Path, emails: list[str]) -> dict[str, list[str]]:
    """Your commits (authored, or naming you in the message) mapped to their parents.
    Needs commits only, so it works on a commits-only clone."""
    found: dict[str, list[str]] = {}
    for filters in mine_filters(emails):
        for line in _git_log(str(repo), ["--format=%H %P"], filters).splitlines():
            commit, *parents = line.split()
            found[commit] = parents
    return found


def share_needed(repo: Path, emails: list[str]) -> float:
    """The share of all commits whose snapshots reading your commits needs: yours, and
    the ones just before each of them."""
    total = int(git(["-C", str(repo), "rev-list", "--all", "--count"]).strip() or 0)
    mine = your_commits(repo, emails)
    needed = set(mine) | {p for parents in mine.values() for p in parents}
    return len(needed) / total if total else 0.0


# The objects fetched so far into a kept clone, so the next run asks only for new ones.
FETCHED_LIST = "ghis-fetched-files"


def fetch_your_files(repo: Path, emails: list[str], env: dict[str, str] | None) -> None:
    """Download what reading your commits needs and the clone left out. For a commits-only
    clone, first the folder listings of the snapshots your commits compare (yours and the
    ones before them); then, for both kinds, the file versions your commits changed, which
    --raw lists from folder listings alone."""
    record = repo / FETCHED_LIST
    fetched = set(record.read_text(encoding="ascii").split()) if record.is_file() else set()
    if config_value(repo, "remote.origin.partialclonefilter") == "tree:0":
        mine = your_commits(repo, emails)
        commits = set(mine) | {p for parents in mine.values() for p in parents}
        roots = (
            set(
                git(
                    ["-C", str(repo), "log", "--no-walk=unsorted", "--format=%T", "--stdin"],
                    stdin="\n".join(sorted(commits)) + "\n",
                ).split()
            )
            if commits
            else set()
        )
        # Each root brings its whole folder listing (--filter=blob:none: no files).
        fetch_objects(repo, roots - fetched, env)
        fetched |= roots
    wanted: set[str] = set()
    for filters in mine_filters(emails):
        out = _git_log(str(repo), ["--no-renames", "--raw", "--no-abbrev", "--format="], filters)
        for line in out.splitlines():
            if not line.startswith(":"):
                continue
            old_mode, new_mode, old, new = line[1:].split("\t", 1)[0].split()[:4]
            # Mode 160000 is a submodule: its id names a commit elsewhere, not a file here.
            if old_mode != "160000":
                wanted.add(old)
            if new_mode != "160000":
                wanted.add(new)
    wanted -= {"0" * 40, "0" * 64}  # the "no file" side of an addition or a deletion
    fetch_objects(repo, wanted - fetched, env)
    record.write_text("\n".join(sorted(fetched | wanted)) + "\n", encoding="ascii")


def fetch_objects(repo: Path, ids: set[str], env: dict[str, str] | None) -> None:
    """Fetch exactly these objects from the clone's remote, in one request."""
    if not ids:
        return
    git(
        [
            "-C",
            str(repo),
            "-c",
            "fetch.negotiationAlgorithm=noop",  # ask for exactly these, nothing else
            "fetch",
            "origin",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            "--filter=blob:none",
            "--stdin",
        ],
        env=env,
        stdin="\n".join(sorted(ids)) + "\n",
    )


def clone_url(project: dict[str, Any], via: str) -> str | None:
    if project.get("cloneUrl"):
        return project["cloneUrl"]
    if via == "ssh":
        return project.get("sshUrl") or project.get("httpUrl")
    return project.get("httpUrl")


def resolve_path(path: str) -> str:
    """numstat prints renames as `a/{old => new}/b` or `old => new`; keep the new path."""
    if "=>" not in path:
        return path
    if "{" in path:
        return RENAME_BRACES.sub(lambda m: m.group(2), path).replace("//", "/")
    return path.split(" => ", 1)[1]


def language_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith("dockerfile"):
        return "Docker"
    if name in FILENAMES:
        return FILENAMES[name]
    return LANGUAGES.get(os.path.splitext(name)[1], "Other")


def _git_log(
    repo: str, output: list[str], filters: list[str], env: dict[str, str] | None = None
) -> str:
    return git(
        [
            "-C",
            repo,
            "-c",
            "core.quotePath=false",
            "log",
            "--exclude=refs/stash",
            "--exclude=refs/notes/*",
            "--all",
            *output,
            "--fixed-strings",
            "--regexp-ignore-case",
            *filters,
        ],
        env=env,
    )


def _log(repo: str, fmt: str, filters: list[str], env: dict[str, str] | None = None) -> str:
    return _git_log(repo, ["--numstat", f"--format={fmt}"], filters, env)


def mine_filters(emails: list[str]) -> tuple[list[str], list[str]]:
    """git log filters for your commits: the ones you authored, and the ones whose message
    names you (scan_repo keeps those with a real Co-authored-by trailer)."""
    return [f"--author={e}" for e in emails], [f"--grep={e}" for e in emails]


def _parse(out: str, ignore: tuple[str, ...], trailers: bool) -> list[dict[str, Any]]:
    commits = []
    fields = 6 if trailers else 5
    for chunk in out.split("\x1e")[1:]:
        header, _, body = chunk.partition("\n")
        parts = header.split("\x1f", fields - 1)
        if len(parts) < fields:
            continue
        sha, parents, when, email = parts[:4]
        co_authors = parts[4].lower() if trailers else ""
        subject = parts[-1]
        adds = dels = files = 0
        langs: dict[str, int] = {}
        for line in body.splitlines():
            cols = line.split("\t", 2)
            if len(cols) != 3:
                continue
            added, deleted, path = cols
            path = resolve_path(path)
            files += 1
            if added == "-" or any(fnmatch.fnmatchcase(path, p) for p in ignore):
                continue
            a, d = int(added), int(deleted)
            adds += a
            dels += d
            lang = language_of(path)
            langs[lang] = langs.get(lang, 0) + a + d
        commit = {
            "h": sha,
            "t": when,
            "e": email.lower(),
            "a": adds,
            "d": dels,
            "f": files,
            "s": subject[:200],
            "m": 1 if len(parents.split()) > 1 else 0,
            "l": langs,
        }
        if trailers:
            commit["_co"] = co_authors
        commits.append(commit)
    return commits


def scan_repo(
    repo: str, emails: list[str], ignore: tuple[str, ...], env: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Your commits in one repository: the ones you authored, plus the ones that credit you
    in a Co-authored-by trailer (pair programming, suggested changes), marked "co"."""
    by_author, by_message = mine_filters(emails)
    authored = _parse(_log(repo, LOG_FORMAT, by_author, env), ignore, False)
    # --grep finds the email anywhere in the message; the trailer check below keeps only real
    # co-authorship, and commits you also authored are already in the first list.
    mentioned = _parse(_log(repo, CO_FORMAT, by_message, env), ignore, True)
    mine = set(emails)
    co_authored = []
    for commit in mentioned:
        trailer = commit.pop("_co")
        if commit["e"] not in mine and any(f"<{e}>" in trailer for e in mine):
            commit["co"] = 1
            co_authored.append(commit)
    return authored + co_authored


def last_fetch(repo: str) -> str | None:
    """When a checkout last fetched from its remote (FETCH_HEAD's time), or None if never:
    the story can only be as current as that."""
    try:
        common = git(["-C", repo, "rev-parse", "--git-common-dir"]).strip()
    except GitError:
        return None
    head = Path(repo) / common / "FETCH_HEAD"
    if not head.is_file():
        return None
    return datetime.fromtimestamp(head.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def check_git() -> None:
    if not shutil.which("git"):
        die("git is not installed or not on PATH. Install it from https://git-scm.com.")
    match = re.search(r"(\d+)\.(\d+)", git(["--version"]))
    if match and (int(match.group(1)), int(match.group(2))) < MIN_GIT:
        die(f"ghis needs git {MIN_GIT[0]}.{MIN_GIT[1]} or newer; you have {match.group(0)}.")
