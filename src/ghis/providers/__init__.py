"""The hosting providers ghis can read, and their collectors."""

from __future__ import annotations

from .gitea import collect_gitea
from .github import collect_github
from .gitlab import collect_gitlab

PROVIDERS: dict[str, dict[str, str]] = {
    "github": {"label": "GitHub", "url": "https://github.com", "token_env": "GITHUB_TOKEN"},
    "gitlab": {"label": "GitLab", "url": "https://gitlab.com", "token_env": "GITLAB_TOKEN"},
    "gitea": {
        "label": "Codeberg / Forgejo / Gitea",
        "url": "https://codeberg.org",
        "token_env": "GITEA_TOKEN",
    },
    "git": {"label": "Any git host", "url": "", "token_env": "GHIS_TOKEN"},
}


COLLECTORS = {"gitlab": collect_gitlab, "github": collect_github, "gitea": collect_gitea}
