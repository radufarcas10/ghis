"""Everything that runs git: finding checkouts, cloning and reading commits."""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
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


RENAME_BRACES = re.compile(r"\{([^{}]*) => ([^{}]*)\}")


class GitError(Exception):
    pass


# Seconds any single git command may run; a hosted server sets it, the CLI leaves it off.
GIT_TIMEOUT: float | None = None


MIN_GIT = (2, 31)  # GIT_CONFIG_COUNT, used to pass the clone token, arrived in 2.31


def git(args: list[str], env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            env=env,
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


def sync_clone(project: dict[str, Any], cache: Path, via: str, env: dict[str, str]) -> Path:
    dest = cache / (re.sub(r"[^\w.-]+", "__", project["path"]) + ".git")
    url = clone_url(project, via)
    if not url:
        raise GitError("no clone URL")
    if dest.exists():
        git(
            [
                "-C",
                str(dest),
                "fetch",
                "--quiet",
                "--prune",
                "--force",
                url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            env=env,
        )
    else:
        partial = dest.with_suffix(".partial")
        if partial.exists():
            shutil.rmtree(partial)
        git(["clone", "--bare", "--quiet", url, str(partial)], env=env)
        partial.rename(dest)
    return dest


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


def scan_repo(repo: str, emails: list[str], ignore: tuple[str, ...]) -> list[dict[str, Any]]:
    args = [
        "-C",
        repo,
        "-c",
        "core.quotePath=false",
        "log",
        "--exclude=refs/stash",
        "--exclude=refs/notes/*",
        "--all",
        "--numstat",
        "--fixed-strings",
        "--regexp-ignore-case",
        f"--format={LOG_FORMAT}",
    ]
    args += [f"--author={email}" for email in emails]
    out = git(args)

    commits = []
    for chunk in out.split("\x1e")[1:]:
        header, _, body = chunk.partition("\n")
        parts = header.split("\x1f", 4)
        if len(parts) < 5:
            continue
        sha, parents, when, email, subject = parts
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
        commits.append(
            {
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
        )
    return commits


def check_git() -> None:
    if not shutil.which("git"):
        die("git is not installed or not on PATH. Install it from https://git-scm.com.")
    match = re.search(r"(\d+)\.(\d+)", git(["--version"]))
    if match and (int(match.group(1)), int(match.group(2))) < MIN_GIT:
        die(f"ghis needs git {MIN_GIT[0]}.{MIN_GIT[1]} or newer; you have {match.group(0)}.")
