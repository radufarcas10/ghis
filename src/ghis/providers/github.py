"""GitHub.com and GitHub Enterprise Server (REST for the user, GraphQL for the rest)."""

from __future__ import annotations

import argparse
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from ..api import ApiError, Http
from ..common import bump, log

GH_REPO_FIELDS = "nameWithOwner name url sshUrl createdAt pushedAt"


GH_PR_FIELDS = (
    "number title state createdAt mergedAt closedAt url author { login } "
    "comments { totalCount } repository { " + GH_REPO_FIELDS + " }"
)


def github_api_urls(url: str) -> tuple[str, str]:
    host = urllib.parse.urlparse(url).hostname
    if host in (None, "github.com", "api.github.com"):
        return "https://api.github.com", "https://api.github.com/graphql"
    return f"{url}/api/v3", f"{url}/api/graphql"


def github_graphql(http: Http, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    data = http.request("", body={"query": query, "variables": variables})[0]
    if data.get("errors"):
        raise ApiError("; ".join(e.get("message", "?") for e in data["errors"]))
    return data["data"]


def github_connection(
    http: Http, login: str, window: tuple[str, str] | None, connection: str, fields: str
) -> Iterator[dict[str, Any]]:
    """Walk a paginated connection on the user, or on its contributionsCollection."""
    after = None
    while True:
        node = (
            f"{connection}(first: 100, after: $after) "
            f"{{ pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }}"
        )
        if window:
            query = (
                "query($login: String!, $after: String, $from: DateTime!, $to: DateTime!) {"
                " user(login: $login) { contributionsCollection(from: $from, to: $to) { "
                + node
                + " } } }"
            )
            variables = {"login": login, "after": after, "from": window[0], "to": window[1]}
        else:
            query = (
                "query($login: String!, $after: String) { user(login: $login) { " + node + " } }"
            )
            variables = {"login": login, "after": after}
        user = github_graphql(http, query, variables)["user"]
        page = (user["contributionsCollection"] if window else user)[connection.split("(")[0]]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


def github_request(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": pr["repository"]["nameWithOwner"],
        "number": pr["number"],
        "title": (pr.get("title") or "")[:200],
        "state": {"OPEN": "opened", "MERGED": "merged", "CLOSED": "closed"}.get(pr["state"]),
        "createdAt": pr.get("createdAt"),
        "mergedAt": pr.get("mergedAt"),
        "closedAt": pr.get("closedAt"),
        "notes": (pr.get("comments") or {}).get("totalCount") or 0,
        "author": (pr.get("author") or {}).get("login"),
        "url": pr.get("url"),
    }


def github_project(repo: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": repo["nameWithOwner"],
        "name": repo["name"],
        "webUrl": repo["url"],
        "sshUrl": repo.get("sshUrl"),
        "httpUrl": repo["url"] + ".git",
        "createdAt": repo.get("createdAt"),
        "lastActivityAt": repo.get("pushedAt"),
        "source": "api",
    }


def year_windows(created: str) -> list[tuple[str, str]]:
    """contributionsCollection spans at most one year, so walk calendar years."""
    start = datetime.fromisoformat(created.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    windows = []
    for year in range(start.year, now.year + 1):
        lo = max(start, datetime(year, 1, 1, tzinfo=timezone.utc))
        hi = min(now, datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
        windows.append((lo.isoformat(), hi.isoformat()))
    return windows


def collect_github(
    url: str, token: str, args: argparse.Namespace, warnings: list[str]
) -> dict[str, Any]:
    rest_url, graphql_url = github_api_urls(url)
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    rest = Http(rest_url, headers, args.insecure)
    gql = Http(graphql_url, headers, args.insecure)

    me = rest.request("/user")[0]
    login = me["login"]
    log(f"Signed in to GitHub as @{login} ({me.get('name')})")

    noreply_host = "users.noreply.github.com"
    emails = {f"{me['id']}+{login}@{noreply_host}".lower(), f"{login}@{noreply_host}".lower()}
    if me.get("email"):
        emails.add(me["email"].lower())
    try:
        emails |= {e["email"].lower() for e in rest.request("/user/emails", {"per_page": 100})[0]}
    except ApiError as err:
        warnings.append(
            f"Could not list your emails ({err}); grant the user:email scope or add them "
            "with --email."
        )

    repos: dict[str, dict[str, Any]] = {}
    activity: dict[str, dict[str, int]] = {}

    log("Reading pull requests you opened...")
    requests = []
    for pr in github_connection(
        gql, login, None, "pullRequests(orderBy: {field: CREATED_AT, direction: ASC})", GH_PR_FIELDS
    ):
        requests.append(github_request(pr))
        repos[pr["repository"]["nameWithOwner"]] = pr["repository"]
        bump(activity, pr.get("createdAt"), "request_opened")
    log(f"  {len(requests)} pull requests")

    log("Reading your contributions year by year...")
    reviews: dict[str, dict[str, Any]] = {}
    for window in year_windows(me["created_at"]):
        year = window[0][:4]
        query = (
            "query($login: String!, $from: DateTime!, $to: DateTime!) { user(login: $login) {"
            " contributionsCollection(from: $from, to: $to) {"
            " commitContributionsByRepository(maxRepositories: 100) { repository { "
            + GH_REPO_FIELDS
            + " } } } } }"
        )
        collection = github_graphql(
            gql, query, {"login": login, "from": window[0], "to": window[1]}
        )
        for item in collection["user"]["contributionsCollection"][
            "commitContributionsByRepository"
        ]:
            repos[item["repository"]["nameWithOwner"]] = item["repository"]

        for node in github_connection(
            gql,
            login,
            window,
            "pullRequestReviewContributions",
            "occurredAt pullRequestReview { state } pullRequest { " + GH_PR_FIELDS + " }",
        ):
            state = (node.get("pullRequestReview") or {}).get("state")
            bump(activity, node["occurredAt"], "approved" if state == "APPROVED" else "reviewed")
            pr = node["pullRequest"]
            if (pr.get("author") or {}).get("login") != login:
                review = github_request(pr)
                review["createdAt"] = node["occurredAt"]
                reviews.setdefault(f"{review['project']}#{review['number']}", review)

        for node in github_connection(gql, login, window, "issueContributions", "occurredAt"):
            bump(activity, node["occurredAt"], "issue_opened")
        log(f"  {year}: {len(repos)} repos, {len(reviews)} reviews so far")

    if args.all_member_projects:
        for repo in github_connection(
            gql,
            login,
            None,
            "repositories(ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER])",
            GH_REPO_FIELDS,
        ):
            repos[repo["nameWithOwner"]] = repo

    return {
        "user": {
            "name": me.get("name") or login,
            "username": login,
            "createdAt": me.get("created_at"),
            "webUrl": me.get("html_url"),
        },
        "emails": emails,
        "projects": [github_project(r) for r in repos.values()],
        "requests": requests,
        "reviews": list(reviews.values()),
        "activity": activity,
        "has": ["requests", "reviews", "approvals"],
        "cloneUser": "x-access-token",
    }
