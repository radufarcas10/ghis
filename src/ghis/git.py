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


def sync_clone(
    project: dict[str, Any], cache: Path, via: str, env: dict[str, str], emails: list[str]
) -> Path:
    """A bare copy of a repository holding everything needed to read your commits.

    Only history is downloaded at first (commits and folder listings, a "blobless" clone),
    then, in one request, the file versions your own commits changed: numstat compares
    exactly those. A large repository costs a fraction of a full clone, and the result is
    identical. A host that can't serve that (an old server) gets a full clone instead."""
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
                # The promisor remote by name, so the fetch stays blobless.
                "origin" if partial else url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            env=env,
        )
        if partial:
            fetch_your_files(dest, emails, env)
        return dest
    building = dest.with_suffix(".partial")
    if building.exists():
        remove_tree(building)
    git(["clone", "--bare", "--quiet", "--filter=blob:none", url, str(building)], env=env)
    if is_partial(building):  # a server without partial clone sends everything instead
        try:
            fetch_your_files(building, emails, env)
        except GitError as err:
            log(f"  {project['path']}: this host can't send single files ({err}); full clone")
            remove_tree(building)
            git(["clone", "--bare", "--quiet", url, str(building)], env=env)
    building.rename(dest)
    return dest


def is_partial(repo: Path) -> bool:
    """Whether a clone left files on the server to fetch later. Recent git marks the remote
    (remote.origin.promisor); older versions set extensions.partialClone."""
    for key in ("remote.origin.promisor", "extensions.partialClone"):
        try:
            value = git(["-C", str(repo), "config", "--get", key]).strip().lower()
        except GitError:
            continue  # not set
        if value and value != "false":
            return True
    return False


# The files fetched so far into a kept clone, so the next run asks only for new ones.
FETCHED_LIST = "ghis-fetched-files"


def fetch_your_files(repo: Path, emails: list[str], env: dict[str, str]) -> None:
    """Download, in one request, every file version that your commits changed: the ones
    scan_repo will compare. --raw lists them from folder listings alone, without the files."""
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
    record = repo / FETCHED_LIST
    fetched = set(record.read_text(encoding="ascii").split()) if record.is_file() else set()
    missing = sorted(wanted - fetched)
    if missing:
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
            stdin="\n".join(missing) + "\n",
        )
    record.write_text("\n".join(sorted(fetched | wanted)) + "\n", encoding="ascii")


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


def _git_log(repo: str, output: list[str], filters: list[str]) -> str:
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
        ]
    )


def _log(repo: str, fmt: str, filters: list[str]) -> str:
    return _git_log(repo, ["--numstat", f"--format={fmt}"], filters)


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


def scan_repo(repo: str, emails: list[str], ignore: tuple[str, ...]) -> list[dict[str, Any]]:
    """Your commits in one repository: the ones you authored, plus the ones that credit you
    in a Co-authored-by trailer (pair programming, suggested changes), marked "co"."""
    by_author, by_message = mine_filters(emails)
    authored = _parse(_log(repo, LOG_FORMAT, by_author), ignore, False)
    # --grep finds the email anywhere in the message; the trailer check below keeps only real
    # co-authorship, and commits you also authored are already in the first list.
    mentioned = _parse(_log(repo, CO_FORMAT, by_message), ignore, True)
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
