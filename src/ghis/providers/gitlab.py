"""GitLab.com and self-managed GitLab."""

from __future__ import annotations

import argparse
import concurrent.futures
from collections.abc import Iterator
from typing import Any

from ..api import ApiError, Http
from ..common import bump, log


def gitlab_paginate(http: Http, path: str, **params: Any) -> Iterator[dict[str, Any]]:
    params = {"per_page": 100, **params}
    page = 1
    while True:
        data, headers = http.request(path, {**params, "page": page})
        yield from data
        next_page = headers.get("X-Next-Page")
        if not next_page:
            return
        page = int(next_page)


def gitlab_action(event: dict[str, Any]) -> str:
    action = event.get("action_name") or "other"
    if action.startswith("pushed"):
        return "pushed"
    if action == "commented on":
        return "commented"
    if action in ("accepted", "merged"):
        return "merged"
    if action == "opened":
        return "request_opened" if event.get("target_type") == "MergeRequest" else "issue_opened"
    return action.replace(" ", "_")


def gitlab_request(mr: dict[str, Any]) -> dict[str, Any]:
    reference = (mr.get("references") or {}).get("full") or ""
    return {
        "project": reference.split("!")[0] or str(mr.get("project_id")),
        "number": mr.get("iid"),
        "title": (mr.get("title") or "")[:200],
        "state": mr.get("state"),
        "createdAt": mr.get("created_at"),
        "mergedAt": mr.get("merged_at"),
        "closedAt": mr.get("closed_at"),
        "notes": mr.get("user_notes_count") or 0,
        "author": (mr.get("author") or {}).get("username"),
        "reviewers": [u["username"] for u in mr.get("reviewers") or [] if u.get("username")],
        "url": mr.get("web_url"),
    }


def gitlab_project(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": project["path_with_namespace"],
        "name": project.get("name") or project["path_with_namespace"].split("/")[-1],
        "webUrl": project.get("web_url"),
        "sshUrl": project.get("ssh_url_to_repo"),
        "httpUrl": project.get("http_url_to_repo"),
        "createdAt": project.get("created_at"),
        "lastActivityAt": project.get("last_activity_at"),
        "source": "api",
    }


def collect_gitlab(
    url: str, token: str, args: argparse.Namespace, warnings: list[str]
) -> dict[str, Any]:
    http = Http(url + "/api/v4", {"PRIVATE-TOKEN": token}, args.insecure)
    me = http.request("/user")[0]
    log(f"Signed in to GitLab as @{me['username']} ({me.get('name')})")

    emails = {me[k].lower() for k in ("email", "public_email", "commit_email") if me.get(k)}
    try:
        emails |= {e["email"].lower() for e in gitlab_paginate(http, "/user/emails")}
    except ApiError as err:
        warnings.append(f"Could not list your secondary emails ({err}); add them with --email.")

    log("Reading your activity feed (this is the slow part)...")
    activity: dict[str, dict[str, int]] = {}
    contributed_ids: set[int] = set()
    count = 0
    for event in gitlab_paginate(http, "/events"):
        action = gitlab_action(event)
        bump(activity, event.get("created_at"), action)
        if action == "pushed" and event.get("project_id"):
            contributed_ids.add(event["project_id"])
        count += 1
        if count % 2000 == 0:
            log(f"  {count} events so far (back to {(event.get('created_at') or '')[:10]})")
    log(f"  {count} events")
    if activity:
        warnings.append(
            f"GitLab returned activity events back to {min(activity)}. Anything older was "
            "pruned by the instance's retention policy; commits are unaffected."
        )

    log("Reading merge requests you opened and reviewed...")
    authored_raw = list(
        gitlab_paginate(http, "/merge_requests", scope="all", author_id=me["id"], state="all")
    )
    contributed_ids |= {mr["project_id"] for mr in authored_raw if mr.get("project_id")}
    try:
        reviewed_raw = list(
            gitlab_paginate(http, "/merge_requests", scope="all", reviewer_id=me["id"], state="all")
        )
    except ApiError as err:
        reviewed_raw = []
        warnings.append(f"Could not list merge requests you reviewed ({err}).")
    requests = [gitlab_request(mr) for mr in authored_raw]
    reviews = [gitlab_request(mr) for mr in reviewed_raw]
    reviews = [r for r in reviews if r["author"] != me.get("username")]
    log(f"  {len(requests)} opened, {len(reviews)} reviewed")

    log("Discovering your projects...")
    projects: dict[int, dict[str, Any]] = {}
    try:
        for project in gitlab_paginate(
            http, f"/users/{me['id']}/contributed_projects", simple="true"
        ):
            projects[project["id"]] = project
    except ApiError as err:
        warnings.append(f"Could not list contributed projects ({err}).")
    if args.all_member_projects:
        for project in gitlab_paginate(http, "/projects", membership="true", simple="true"):
            projects[project["id"]] = project
    contributed_ids |= set(projects)

    def fetch(pid: int) -> dict[str, Any] | None:
        try:
            return http.request(f"/projects/{pid}")[0]
        except ApiError as err:
            if err.status in (403, 404):
                return None
            raise

    missing = [pid for pid in contributed_ids if pid not in projects]
    gone = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for project in pool.map(fetch, missing):
            if project:
                projects[project["id"]] = project
            else:
                gone += 1
    if gone:
        warnings.append(
            f"{gone} project(s) from your activity are deleted or no longer visible to you; "
            "their commits cannot be read."
        )
    log(f"  {len(projects)} projects")

    return {
        "user": {
            "name": me.get("name"),
            "username": me.get("username"),
            "createdAt": me.get("created_at"),
            "webUrl": me.get("web_url"),
        },
        "emails": emails,
        "projects": [gitlab_project(p) for p in projects.values()],
        "requests": requests,
        "reviews": reviews,
        "activity": activity,
        "has": ["requests", "reviews", "approvals", "comments"],
        "cloneUser": "oauth2",
    }
