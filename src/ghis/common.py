"""Shared basics: paths, logging, the error collectors raise, host checks."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
WEB_DIR = PACKAGE / "web"


def _data_dir() -> Path:
    """Where out/ (stories) and cache/ (clones) go: $GHIS_HOME, else the checkout when
    running from source (python3 ghis.py), else ~/.ghis for an installed ghis."""
    if os.environ.get("GHIS_HOME"):
        return Path(os.environ["GHIS_HOME"]).expanduser()
    checkout = PACKAGE.parent.parent
    if (checkout / "ghis.py").is_file() and (checkout / "pyproject.toml").is_file():
        return checkout
    return Path.home() / ".ghis"


DATA_DIR = _data_dir()


WEB_FILES = ("index.html", "style.css", "lib.js", "app.js", "theme.js", "favicon.svg")


SCHEMA_VERSION = 1


_sink = threading.local()


class CollectError(Exception):
    pass


def log(message: str) -> None:
    job = getattr(_sink, "job", None)
    if job is not None:
        job.progress(message)
    else:
        print(f"[ghis] {message}", file=sys.stderr, flush=True)


def die(message: str) -> None:
    raise CollectError(message)


def bump(activity: dict[str, dict[str, int]], when: str | None, action: str) -> None:
    """Count one activity event under its UTC hour ("2024-03-01T09"). The page moves the hour
    into the author's own timezone, so a late-evening review lands on the same day as the
    commits beside it. Timestamps without a timezone keep their day."""
    if not when:
        return
    try:
        moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        key = when[:10]
    else:
        key = (
            moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H") if moment.tzinfo else when[:10]
        )
    bucket = activity.setdefault(key, {})
    bucket[action] = bucket.get(action, 0) + 1


def same_host(repo_host: str | None, host: str | None) -> bool:
    """Whether a remote lives on an instance; ssh.github.com counts as github.com."""
    if not repo_host or not host:
        return False
    repo_host, host = repo_host.lower(), host.lower()
    return repo_host == host or repo_host.endswith("." + host)


def host_allowed(host: str | None, allowed: set[str] | None) -> bool:
    if allowed is None or "*" in allowed:
        return True
    return bool(host) and any(host == a or host.endswith("." + a) for a in allowed)


def tilde(path: Path) -> str:
    """Show a path under the home folder as ~/..., the way people type it."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
