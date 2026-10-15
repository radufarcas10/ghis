"""Provider tests against a fake host that replays the APIs' response shapes, including
pagination, a deleted project and other people's pull requests. Nothing touches the network."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ghis import collect as collect_mod  # noqa: E402
from ghis.common import CollectError  # noqa: E402
from ghis.providers.github import year_windows  # noqa: E402
from test_ghis import Isolated, make_repo  # noqa: E402

Route = Callable[[dict[str, str]], Any]  # query -> JSON body, or (status, body, headers)


class FakeHost:
    """A tiny HTTP server: GET routes by path, POST /api/graphql to a handler."""

    def __init__(
        self, routes: dict[str, Route], graphql: Callable[[str, dict], dict] | None = None
    ):
        # (method, path, headers with lower-case names, as HTTP treats them)
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def reply(self, result: Any) -> None:
                status, body, headers = result if isinstance(result, tuple) else (200, result, {})
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                url = urllib.parse.urlparse(self.path)
                host.seen.append(("GET", url.path, {k.lower(): v for k, v in self.headers.items()}))
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                route = routes.get(url.path)
                self.reply(route(query) if route else (404, {"message": "404 Not Found"}, {}))

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                host.seen.append(
                    ("POST", self.path, {k.lower(): v for k, v in self.headers.items()})
                )
                self.reply({"data": graphql(body["query"], body["variables"])})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def gitlab_pages(*pages: list) -> Route:
    """GitLab-style offset pagination: X-Next-Page until the last page."""

    def route(query: dict[str, str]) -> Any:
        index = int(query.get("page", "1")) - 1
        headers = {"X-Next-Page": str(index + 2)} if index + 1 < len(pages) else {}
        return 200, pages[index] if index < len(pages) else [], headers

    return route


def gitea_pages(*pages: list) -> Route:
    """Gitea-style: pages until an empty one."""
    return lambda query: (list(pages) + [[]] * 5)[int(query.get("page", "1")) - 1]


def options(**overrides: Any) -> argparse.Namespace:
    base = dict(
        url=None,
        email=[],
        repo=[],
        name=None,
        clone_user="git",
        local_root=[],
        depth=4,
        no_clone=True,
        clone_via="https",
        cache="",
        all_member_projects=False,
        count_all_files=False,
        jobs=2,
        insecure=False,
        token_file=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def by_day(activity: dict[str, dict[str, int]], action: str) -> int:
    return sum(day.get(action, 0) for day in activity.values())


# --------------------------------------------------------------------------- GitLab


def gitlab_project(pid: int, path: str, repo: Path | None = None) -> dict[str, Any]:
    # file:// like a real host: a plain path makes git copy the folder, skipping the protocol.
    clone = repo.as_uri() if repo else f"https://gitlab.example/{path}.git"
    return {
        "id": pid,
        "path_with_namespace": path,
        "name": path.split("/")[-1],
        "web_url": f"https://gitlab.example/{path}",
        "ssh_url_to_repo": clone,
        "http_url_to_repo": clone,
        "created_at": "2021-01-01T00:00:00Z",
        "last_activity_at": "2024-05-01T00:00:00Z",
    }


def gitlab_mr(
    iid: int,
    project: str,
    author: str,
    state: str,
    created: str,
    merged: str | None = None,
    project_id: int = 1,
) -> dict[str, Any]:
    return {
        "iid": iid,
        "project_id": project_id,
        "title": f"MR {iid}",
        "state": state,
        "created_at": created,
        "merged_at": merged,
        "closed_at": None,
        "user_notes_count": 3,
        "author": {"username": author},
        "reviewers": [{"username": "bob"}, {"username": "cleo"}] if author == "ada" else [],
        "web_url": f"https://gitlab.example/{project}/-/merge_requests/{iid}",
        "references": {"full": f"{project}!{iid}"},
    }


def fake_gitlab(repo: Path | None = None) -> FakeHost:
    def merge_requests(query: dict[str, str]) -> Any:
        if query.get("author_id") == "7":
            return gitlab_pages(
                [
                    gitlab_mr(
                        1,
                        "team/app",
                        "ada",
                        "merged",
                        "2024-03-01T10:00:00Z",
                        "2024-03-01T15:00:00Z",
                    ),
                    gitlab_mr(2, "team/api", "ada", "opened", "2024-03-02T10:00:00Z", project_id=2),
                ]
            )(query)
        # Reviewer list: one of Ada's own MRs comes back too and must not count as a review.
        return gitlab_pages(
            [
                gitlab_mr(
                    9, "team/app", "bob", "merged", "2024-03-03T10:00:00Z", "2024-03-04T10:00:00Z"
                ),
                gitlab_mr(
                    1, "team/app", "ada", "merged", "2024-03-01T10:00:00Z", "2024-03-01T15:00:00Z"
                ),
            ]
        )(query)

    routes: dict[str, Route] = {
        "/api/v4/user": lambda q: {
            "id": 7,
            "username": "ada",
            "name": "Ada Example",
            "email": "ME@example.com",
            "created_at": "2020-02-02T00:00:00Z",
            "web_url": "https://gitlab.example/ada",
        },
        "/api/v4/user/emails": gitlab_pages([{"id": 1, "email": "ada@old-job.example"}]),
        "/api/v4/events": gitlab_pages(
            [
                {"action_name": "pushed to", "project_id": 1, "created_at": "2024-03-01T09:00:00Z"},
                {
                    "action_name": "commented on",
                    "target_type": "Note",
                    "project_id": 1,
                    "created_at": "2024-03-01T11:00:00Z",
                },
                {
                    "action_name": "approved",
                    "target_type": "MergeRequest",
                    "project_id": 1,
                    "created_at": "2024-03-04T09:00:00Z",
                },
            ],
            [
                {
                    "action_name": "opened",
                    "target_type": "MergeRequest",
                    "project_id": 1,
                    "created_at": "2024-03-01T10:00:00Z",
                },
                {
                    "action_name": "accepted",
                    "target_type": "MergeRequest",
                    "project_id": 1,
                    "created_at": "2024-03-01T15:00:00Z",
                },
                {"action_name": "pushed to", "project_id": 3, "created_at": "2023-01-01T00:00:00Z"},
            ],
        ),
        "/api/v4/merge_requests": merge_requests,
        "/api/v4/users/7/contributed_projects": gitlab_pages([gitlab_project(1, "team/app", repo)]),
        "/api/v4/projects/2": lambda q: gitlab_project(2, "team/api"),
        # Project 3 (from an old push) was deleted: the default route answers 404.
    }
    return FakeHost(routes)


class GitLabTests(Isolated):
    def test_collects_requests_reviews_activity_and_projects(self) -> None:
        host = fake_gitlab()
        self.addCleanup(host.close)
        data = collect_mod.collect(options(url=host.url), "gitlab", "glpat-secret")

        self.assertEqual(data["user"]["username"], "ada")
        self.assertEqual(data["source"]["emails"], ["ada@old-job.example", "me@example.com"])
        self.assertEqual(
            [(r["number"], r["state"], r["project"]) for r in data["requests"]],
            [(1, "merged", "team/app"), (2, "opened", "team/api")],
        )
        self.assertEqual([r["number"] for r in data["reviews"]], [9])  # own MR filtered out
        self.assertEqual(data["requests"][0]["reviewers"], ["bob", "cleo"])
        # Activity is kept per UTC hour, for the page to move into the author's timezone.
        self.assertTrue(all(len(day) == 13 for day in data["activity"]), list(data["activity"])[:3])
        self.assertEqual(by_day(data["activity"], "commented"), 1)
        self.assertEqual(by_day(data["activity"], "approved"), 1)
        self.assertEqual(by_day(data["activity"], "request_opened"), 1)
        self.assertEqual(by_day(data["activity"], "merged"), 1)
        self.assertEqual(sorted(p["path"] for p in data["projects"]), ["team/api", "team/app"])
        self.assertTrue(any("1 project(s)" in w for w in data["warnings"]), data["warnings"])
        self.assertTrue(any("events back to 2023-01-01" in w for w in data["warnings"]))
        # The token goes in the header GitLab expects, on every call.
        self.assertTrue(all(h.get("private-token") == "glpat-secret" for _, _, h in host.seen))

    def test_clones_contributed_projects_and_reads_commits(self) -> None:
        repo = make_repo(self.root / "remote", "app")
        host = fake_gitlab(repo)
        self.addCleanup(host.close)
        args = options(url=host.url, no_clone=False, cache=str(self.root / "cache"))
        data = collect_mod.collect(args, "gitlab", "glpat-secret")
        paths = [p["path"] for p in data["projects"]]
        app = paths.index("team/app")
        self.assertEqual(len([c for c in data["commits"] if c["r"] == app]), 3)
        self.assertEqual(data["source"]["clonedRepos"], 1)
        # A second run fetches into the kept clone instead of cloning again.
        again = collect_mod.collect(args, "gitlab", "glpat-secret")
        self.assertEqual(len(again["commits"]), len(data["commits"]))

    def test_downloads_are_deleted_after_the_story(self) -> None:
        repo = make_repo(self.root / "remote-temp", "app")
        host = fake_gitlab(repo)
        self.addCleanup(host.close)
        work = self.root / "work"
        work.mkdir()
        # A killed run's leftovers: a day-old one is swept, a fresh one may still be in use.
        stale, fresh = work / "ghis-clones-stale", work / "ghis-clones-fresh"
        stale.mkdir()
        fresh.mkdir()
        os.utime(stale, (time.time() - 2 * 86400,) * 2)
        args = options(url=host.url, no_clone=False, cache="", workdir=str(work))
        data = collect_mod.collect(args, "gitlab", "glpat-secret")
        self.assertEqual(data["source"]["clonedRepos"], 1)
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["ghis-clones-fresh"])


# --------------------------------------------------------------------------- GitHub


def github_repo(name: str) -> dict[str, Any]:
    return {
        "nameWithOwner": name,
        "name": name.split("/")[1],
        "url": f"https://github.example/{name}",
        "sshUrl": f"git@github.example:{name}.git",
        "createdAt": "2022-01-01T00:00:00Z",
        "pushedAt": "2024-01-01T00:00:00Z",
    }


def github_pr(
    number: int, repo: str, author: str, state: str, created: str, merged: str | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "createdAt": created,
        "mergedAt": merged,
        "closedAt": merged,
        "url": f"https://github.example/{repo}/pull/{number}",
        "author": {"login": author},
        "comments": {"totalCount": 2},
        "reviews": {
            "nodes": [
                {"author": {"login": "bob"}},
                {"author": {"login": "bob"}},
                {"author": {"login": author}},  # replying to a review is not reviewing
            ]
        },
        "repository": github_repo(repo),
    }


def fake_github() -> FakeHost:
    def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        year = (variables.get("from") or "")[:4]
        if "pullRequests(" in query:
            if variables["after"] is None:
                nodes = [
                    github_pr(
                        1,
                        "ada/site",
                        "ada",
                        "MERGED",
                        "2024-02-01T10:00:00Z",
                        "2024-02-02T10:00:00Z",
                    )
                ]
                page = {"hasNextPage": True, "endCursor": "c1"}
            else:
                nodes = [github_pr(2, "acme/tool", "ada", "CLOSED", "2024-02-03T10:00:00Z")]
                page = {"hasNextPage": False, "endCursor": None}
            return {"user": {"pullRequests": {"pageInfo": page, "nodes": nodes}}}
        if "commitContributionsByRepository" in query:
            repos = [{"repository": github_repo("ada/dotfiles")}] if year == "2024" else []
            return {"user": {"contributionsCollection": {"commitContributionsByRepository": repos}}}
        if "pullRequestReviewContributions" in query:
            nodes = []
            if year == "2024":
                pr = github_pr(
                    7, "acme/tool", "bob", "MERGED", "2024-04-01T10:00:00Z", "2024-04-02T10:00:00Z"
                )
                nodes = [
                    {
                        "occurredAt": "2024-04-01T12:00:00Z",
                        "pullRequestReview": {"state": "COMMENTED"},
                        "pullRequest": pr,
                    },
                    {
                        "occurredAt": "2024-04-01T18:00:00Z",
                        "pullRequestReview": {"state": "APPROVED"},
                        "pullRequest": pr,
                    },
                ]
            collection = {
                "pullRequestReviewContributions": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
            return {"user": {"contributionsCollection": collection}}
        if "issueContributions" in query:
            nodes = [{"occurredAt": "2024-05-05T10:00:00Z"}] if year == "2024" else []
            collection = {
                "issueContributions": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                }
            }
            return {"user": {"contributionsCollection": collection}}
        raise AssertionError(f"unexpected query: {query[:80]}")

    routes: dict[str, Route] = {
        "/api/v3/user": lambda q: {
            "login": "ada",
            "id": 42,
            "name": "Ada Example",
            "created_at": "2023-06-01T00:00:00Z",
            "email": None,
            "html_url": "https://github.example/ada",
        },
        "/api/v3/user/emails": lambda q: [{"email": "ada@example.com", "primary": True}],
    }
    return FakeHost(routes, graphql)


class GitHubTests(Isolated):
    def test_collects_pull_requests_reviews_and_repositories(self) -> None:
        host = fake_github()
        self.addCleanup(host.close)
        data = collect_mod.collect(options(url=host.url), "github", "ghp_secret")

        self.assertEqual(data["user"]["username"], "ada")
        self.assertIn("42+ada@users.noreply.github.com", data["source"]["emails"])
        self.assertIn("ada@example.com", data["source"]["emails"])
        self.assertEqual(
            [(r["number"], r["state"]) for r in data["requests"]], [(1, "merged"), (2, "closed")]
        )
        # Two reviews of one PR are one reviewed PR, but both count as activity.
        self.assertEqual([r["number"] for r in data["reviews"]], [7])
        self.assertEqual(data["requests"][0]["reviewers"], ["bob"])  # deduped, author excluded
        self.assertEqual(by_day(data["activity"], "reviewed"), 1)
        self.assertEqual(by_day(data["activity"], "approved"), 1)
        self.assertEqual(by_day(data["activity"], "issue_opened"), 1)
        self.assertEqual(by_day(data["activity"], "request_opened"), 2)
        self.assertEqual(sorted(p["path"] for p in data["projects"]), ["acme/tool", "ada/site"])
        self.assertTrue(all(h.get("authorization") == "Bearer ghp_secret" for _, _, h in host.seen))

    def test_year_windows_cover_every_year_once(self) -> None:
        windows = year_windows("2021-11-15T08:00:00Z")
        self.assertEqual(windows[0][0][:10], "2021-11-15")
        self.assertEqual(
            [w[0][:4] for w in windows], [str(y) for y in range(2021, 2021 + len(windows))]
        )


# --------------------------------------------------------------------------- Gitea / Forgejo


def gitea_repo(name: str) -> dict[str, Any]:
    return {
        "full_name": name,
        "name": name.split("/")[1],
        "html_url": f"https://codeberg.example/{name}",
        "clone_url": f"https://codeberg.example/{name}.git",
        "ssh_url": f"git@codeberg.example:{name}.git",
        "created_at": "2022-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def gitea_issue(number: int, repo: str, author: str, state: str, merged: bool) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "comments": 1,
        "created_at": "2024-06-01T10:00:00Z",
        "closed_at": "2024-06-02T10:00:00Z" if state == "closed" else None,
        "html_url": f"https://codeberg.example/{repo}/pulls/{number}",
        "user": {"login": author},
        "repository": {"full_name": repo},  # search results carry a trimmed repository
        "pull_request": {"merged": merged, "merged_at": "2024-06-02T10:00:00Z" if merged else None},
    }


def fake_gitea() -> FakeHost:
    def search(query: dict[str, str]) -> Any:
        if query.get("created") == "true":
            return gitea_pages(
                [
                    gitea_issue(1, "ada/notes", "ada", "closed", True),
                    gitea_issue(2, "ada/notes", "ada", "open", False),
                ]
            )(query)
        return gitea_pages([gitea_issue(5, "club/site", "bob", "closed", True)])(query)

    routes: dict[str, Route] = {
        "/api/v1/user": lambda q: {
            "login": "ada",
            "full_name": "Ada Example",
            "email": "ada@example.com",
            "created": "2021-01-01T00:00:00Z",
        },
        "/api/v1/user/emails": lambda q: [
            {"email": "ada@example.com"},
            {"email": "a@home.example"},
        ],
        "/api/v1/users/ada/activities/feeds": gitea_pages(
            [
                {
                    "op_type": "commit_repo",
                    "created": "2024-06-01T09:00:00Z",
                    "repo": gitea_repo("ada/notes"),
                },
                {
                    "op_type": "comment_issue",
                    "created": "2024-06-01T11:00:00Z",
                    "repo": gitea_repo("ada/notes"),
                },
            ],
            [
                {
                    "op_type": "approve_pull_request",
                    "created": "2024-06-03T11:00:00Z",
                    "repo": gitea_repo("club/site"),
                }
            ],
        ),
        "/api/v1/repos/issues/search": search,
        "/api/v1/repos/ada/notes": lambda q: gitea_repo("ada/notes"),
    }
    return FakeHost(routes)


class GiteaTests(Isolated):
    def test_collects_pull_requests_reviews_and_activity(self) -> None:
        host = fake_gitea()
        self.addCleanup(host.close)
        data = collect_mod.collect(options(url=host.url), "gitea", "gitea-secret")

        self.assertEqual(data["user"]["name"], "Ada Example")
        self.assertEqual(data["source"]["emails"], ["a@home.example", "ada@example.com"])
        self.assertEqual(
            [(r["number"], r["state"]) for r in data["requests"]], [(1, "merged"), (2, "opened")]
        )
        self.assertEqual([r["number"] for r in data["reviews"]], [5])
        self.assertEqual(by_day(data["activity"], "commented"), 1)
        self.assertEqual(by_day(data["activity"], "approved"), 1)
        self.assertEqual([p["path"] for p in data["projects"]], ["ada/notes"])
        self.assertTrue(
            all(h.get("authorization") == "token gitea-secret" for _, _, h in host.seen)
        )


# --------------------------------------------------------------------------- failures


class FailureTests(Isolated):
    def test_rejected_token_is_a_readable_error(self) -> None:
        host = FakeHost({"/api/v4/user": lambda q: (401, {"message": "401 Unauthorized"}, {})})
        self.addCleanup(host.close)
        with self.assertRaises(CollectError) as caught:
            collect_mod.collect(options(url=host.url), "gitlab", "wrong")
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("wrong", str(caught.exception))  # never echo the token

    def test_redirect_to_another_host_is_refused(self) -> None:
        target = tempfile.mkdtemp()
        host = FakeHost(
            {"/api/v4/user": lambda q: (302, {}, {"Location": f"http://localhost:1/{target}"})}
        )
        self.addCleanup(host.close)
        with self.assertRaises(CollectError) as caught:
            collect_mod.collect(options(url=host.url), "gitlab", "token")
        self.assertIn("redirect", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
