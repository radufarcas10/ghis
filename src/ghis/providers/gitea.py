"""Codeberg, Forgejo and Gitea, which share one API."""

from __future__ import annotations

import argparse
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from ..api import ApiError, Http
from ..common import bump, log

# Activity-feed op types (Gitea and Forgejo share them) mapped to ghis' action names.
GITEA_ACTIONS = {
    "commit_repo": "pushed",
    "create_pull_request": "request_opened",
    "merge_pull_request": "merged",
    "auto_merge_pull_request": "merged",
    "approve_pull_request": "approved",
    "reject_pull_request": "reviewed",
    "comment_issue": "commented",
    "comment_pull": "commented",
    "create_issue": "issue_opened",
}


def gitea_paginate(http: Http, path: str, **params: Any) -> Iterator[dict[str, Any]]:
    # Servers cap `limit` at their own MAX_RESPONSE_ITEMS, so only an empty page means the end.
    page = 1
    while True:
        data = http.request(path, {**params, "limit": 50, "page": page})[0]
        if not data:
            return
        yield from data
        page += 1


def gitea_request(issue: dict[str, Any]) -> dict[str, Any]:
    pull = issue.get("pull_request") or {}
    state = "merged" if pull.get("merged") else ("opened" if issue["state"] == "open" else "closed")
    return {
        "project": (issue.get("repository") or {}).get("full_name") or "?",
        "number": issue.get("number"),
        "title": (issue.get("title") or "")[:200],
        "state": state,
        "createdAt": issue.get("created_at"),
        "mergedAt": pull.get("merged_at"),
        "closedAt": issue.get("closed_at") if state == "closed" else None,
        "notes": issue.get("comments") or 0,
        "author": (issue.get("user") or {}).get("login"),
        "reviewers": [],  # the issue search has no reviewers; fetching each pull would cost a call
        "url": issue.get("html_url"),
    }


def gitea_project(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": repo["full_name"],
        "name": repo.get("name") or repo["full_name"].split("/")[-1],
        "webUrl": repo.get("html_url"),
        "sshUrl": repo.get("ssh_url"),
        "httpUrl": repo.get("clone_url"),
        "createdAt": repo.get("created_at"),
        "lastActivityAt": repo.get("updated_at"),
        "source": "api",
    }


def collect_gitea(
    url: str, token: str, args: argparse.Namespace, warnings: list[str]
) -> dict[str, Any]:
    http = Http(url + "/api/v1", {"Authorization": f"token {token}"}, args.insecure)
    me = http.request("/user")[0]
    login = me["login"]
    log(f"Signed in to {urllib.parse.urlparse(url).hostname} as @{login}")

    emails = {me["email"].lower()} if me.get("email") else set()
    try:
        emails |= {e["email"].lower() for e in http.request("/user/emails")[0]}
    except ApiError as err:
        warnings.append(f"Could not list your emails ({err}); add them with --email.")

    log("Reading your activity feed...")
    activity: dict[str, dict[str, int]] = {}
    repos: dict[str, dict[str, Any]] = {}
    count = 0
    try:
        for event in gitea_paginate(
            http, f"/users/{login}/activities/feeds", **{"only-performed-by": "true"}
        ):
            action = GITEA_ACTIONS.get(event.get("op_type") or "", event.get("op_type") or "other")
            bump(activity, event.get("created"), action)
            repo = event.get("repo") or {}
            if action == "pushed" and repo.get("full_name"):
                repos[repo["full_name"]] = repo
            count += 1
            if count % 1000 == 0:
                log(f"  {count} events so far")
    except ApiError as err:
        warnings.append(
            f"This server has no activity feed API ({err}); the calendar shows commits only."
        )
    log(f"  {count} events")

    log("Reading pull requests you opened and reviewed...")
    requests = [
        gitea_request(i)
        for i in gitea_paginate(
            http, "/repos/issues/search", type="pulls", state="all", created="true"
        )
    ]
    try:
        reviews = [
            gitea_request(i)
            for i in gitea_paginate(
                http, "/repos/issues/search", type="pulls", state="all", reviewed="true"
            )
        ]
        reviews = [r for r in reviews if r["author"] != login]
    except ApiError as err:
        reviews = []
        warnings.append(f"Could not list pull requests you reviewed ({err}).")
    log(f"  {len(requests)} opened, {len(reviews)} reviewed")

    if args.all_member_projects:
        for repo in gitea_paginate(http, "/user/repos"):
            repos[repo["full_name"]] = repo

    # Search results and feed entries can carry a trimmed repository; fetch the full one.
    names = set(repos) | {r["project"] for r in requests}

    def fetch(name: str) -> dict[str, Any] | None:
        if repos.get(name, {}).get("clone_url"):
            return repos[name]
        try:
            return http.request(f"/repos/{name}")[0]
        except ApiError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        full = [r for r in pool.map(fetch, sorted(names)) if r]
    log(f"  {len(full)} repositories")

    return {
        "user": {
            "name": me.get("full_name") or login,
            "username": login,
            "createdAt": me.get("created"),
            "webUrl": me.get("html_url") or f"{url}/{login}",
        },
        "emails": emails,
        "projects": [gitea_project(r) for r in full],
        "requests": requests,
        "reviews": reviews,
        "activity": activity,
        "has": ["requests", "reviews", "approvals", "comments"],
        "cloneUser": login,
    }
