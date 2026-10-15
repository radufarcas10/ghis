"""Tests for ghis. Standard library only: run with `python3 -m unittest discover tests`."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ghis import assets, common, demo, detect, git, server, site  # noqa: E402
from ghis import collect as collect_mod  # noqa: E402


def make_repo(root: Path, name: str) -> Path:
    """A small repository with commits by two authors, a rename and a lockfile."""
    repo = root / name
    repo.mkdir(parents=True)
    run = lambda *args, **env: subprocess.run(  # noqa: E731
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )
    run("init", "-q", "-b", "main")
    run("remote", "add", "origin", f"git@example.com:team/{name}.git")

    def commit(message: str, email: str, when: str) -> None:
        run("add", "-A")
        run(
            "commit",
            "-q",
            "-m",
            message,
            GIT_AUTHOR_NAME="Someone",
            GIT_AUTHOR_EMAIL=email,
            GIT_AUTHOR_DATE=when,
            GIT_COMMITTER_NAME="Someone",
            GIT_COMMITTER_EMAIL=email,
            GIT_COMMITTER_DATE=when,
        )

    lines = [f"value_{i} = {i}" for i in range(10)]
    (repo / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (repo / "package-lock.json").write_text("{}\n" * 500, encoding="utf-8")
    commit("APP-1 Add app", "me@example.com", "2024-03-04T10:00:00+02:00")
    (repo / "src").mkdir()
    (repo / "app.py").rename(repo / "src" / "main.py")
    lines[3] = "value_3 = 'changed'"
    (repo / "src" / "main.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    commit("Move app into src", "ME@Example.com", "2024-03-05T23:30:00+02:00")
    (repo / "other.js").write_text("x\n", encoding="utf-8")
    commit("Someone else's work", "them@example.com", "2024-03-06T09:00:00+02:00")
    (repo / "pair.js").write_text("pair\n", encoding="utf-8")
    commit(
        "Pair on the parser\n\nCo-authored-by: Me <me@example.com>",
        "them@example.com",
        "2024-03-07T11:00:00+02:00",
    )
    (repo / "thanks.js").write_text("thanks\n", encoding="utf-8")
    commit(
        "Credit an idea\n\nThanks to me@example.com for it.",
        "them@example.com",
        "2024-03-08T11:00:00+02:00",
    )
    return repo


class Isolated(unittest.TestCase):
    """Keeps the developer's own git config out of the tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.env = {
            "HOME": str(cls.root),
            "USERPROFILE": str(cls.root),  # what Path.home() reads on Windows
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(cls.root),
        }
        cls.saved = {k: os.environ.get(k) for k in cls.env}
        os.environ.update(cls.env)

    @classmethod
    def tearDownClass(cls) -> None:
        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls.tmp.cleanup()


class HelperTests(unittest.TestCase):
    def test_activity_is_counted_per_utc_hour(self) -> None:
        activity: dict = {}
        for when in (
            "2024-06-01T01:30:00+02:00",
            "2024-03-01T09:00:00.000Z",
            "2024-03-01T09:59:59Z",
        ):
            common.bump(activity, when, "commented")
        common.bump(activity, "2024-01-05", "approved")  # no time: the day stays
        self.assertEqual(
            activity,
            {
                "2024-05-31T23": {"commented": 1},  # 01:30 at +02:00 is the previous UTC day
                "2024-03-01T09": {"commented": 2},
                "2024-01-05": {"approved": 1},
            },
        )

    def test_parse_remote(self) -> None:
        cases = {
            "git@gitlab.com:group/sub/app.git": ("gitlab.com", "group/sub/app"),
            "ssh://git@host:2222/team/app.git": ("host", "team/app"),
            "https://github.com/owner/repo": ("github.com", "owner/repo"),
            "https://user@bitbucket.org/team/app.git": ("bitbucket.org", "team/app"),
            "/just/a/path": (None, None),
        }
        for url, expected in cases.items():
            self.assertEqual(git.parse_remote(url), expected, url)

    def test_resolve_path(self) -> None:
        self.assertEqual(git.resolve_path("src/{old => new}/a.py"), "src/new/a.py")
        self.assertEqual(git.resolve_path("{ => src}/a.py"), "src/a.py")
        self.assertEqual(git.resolve_path("src/{lib => }/a.py"), "src/a.py")
        self.assertEqual(git.resolve_path("a.py => b.py"), "b.py")
        self.assertEqual(git.resolve_path("plain.py"), "plain.py")

    def test_language_of(self) -> None:
        self.assertEqual(git.language_of("app/Thing.vue"), "Vue")
        self.assertEqual(git.language_of("Dockerfile.prod"), "Docker")
        self.assertEqual(git.language_of("Makefile"), "Makefile")
        self.assertEqual(git.language_of("weird.xyz"), "Other")

    def test_host_allowed(self) -> None:
        allowed = {"github.com", "example.org"}
        self.assertTrue(common.host_allowed("github.com", allowed))
        self.assertTrue(common.host_allowed("git.example.org", allowed))
        self.assertFalse(common.host_allowed("evilgithub.com", allowed))
        self.assertFalse(common.host_allowed("169.254.169.254", allowed))
        self.assertFalse(common.host_allowed(None, allowed))
        self.assertTrue(common.host_allowed("anything", None))
        self.assertTrue(common.host_allowed("anything", {"*"}))


def collect_args(**overrides: object) -> argparse.Namespace:
    base = dict(
        url=None,
        email=["me@example.com"],
        repo=[],
        name=None,
        clone_user="git",
        local_root=[],
        depth=4,
        no_clone=True,
        clone_via="ssh",
        cache="",
        all_member_projects=False,
        count_all_files=False,
        jobs=2,
        insecure=False,
        token_file=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class AssetVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "app.js").write_text("one", encoding="utf-8")
        (self.root / "demo").mkdir()
        (self.root / "demo" / "style.css").write_text("two", encoding="utf-8")

    def test_local_references_get_a_fingerprint_of_their_contents(self) -> None:
        html = (
            '<script src="app.js"></script><link href="demo/style.css?v=old" rel="stylesheet">'
            '<a href="https://github.com/x/y.js">x</a><img src="data:image/png;base64,AA">'
            '<script src="missing.js"></script><a href="#top">top</a>'
            '<script src="api/x.js"></script>'
        )
        out = assets.version_refs(html, self.root)
        self.assertIn(f'src="app.js?v={assets.fingerprint(self.root / "app.js")}"', out)
        self.assertIn(
            f'href="demo/style.css?v={assets.fingerprint(self.root / "demo" / "style.css")}"', out
        )
        for untouched in (
            'href="https://github.com/x/y.js"',
            'src="data:image/png;base64,AA"',
            'src="missing.js"',
            'href="#top"',
            'src="api/x.js"',
        ):
            self.assertIn(untouched, out)

    def test_the_fingerprint_changes_exactly_when_the_file_does(self) -> None:
        before = assets.version_refs('<script src="app.js"></script>', self.root)
        self.assertEqual(before, assets.version_refs('<script src="app.js"></script>', self.root))
        (self.root / "app.js").write_text("one, changed", encoding="utf-8")
        self.assertNotEqual(
            before, assets.version_refs('<script src="app.js"></script>', self.root)
        )

    def test_a_built_site_including_absolute_links_and_manifest(self) -> None:
        (self.root / "icon.png").write_bytes(b"png")
        (self.root / "site.webmanifest").write_text(
            '{"icons": [{"src": "icon.png"}]}', encoding="utf-8"
        )
        (self.root / "404.html").write_text(
            '<link href="https://example.test/site/app.js">', encoding="utf-8"
        )
        (self.root / "index.html").write_text(
            '<link rel="manifest" href="site.webmanifest">', encoding="utf-8"
        )
        assets.version_tree(self.root, "https://example.test/site/")
        manifest = (self.root / "site.webmanifest").read_text(encoding="utf-8")
        self.assertIn(f'"src": "icon.png?v={assets.fingerprint(self.root / "icon.png")}"', manifest)
        self.assertIn("app.js?v=", (self.root / "404.html").read_text(encoding="utf-8"))
        # Pages fingerprint the manifest as it ends up, icons stamped.
        stamp = assets.fingerprint(self.root / "site.webmanifest")
        self.assertIn(
            f"site.webmanifest?v={stamp}", (self.root / "index.html").read_text(encoding="utf-8")
        )


class CollectTests(Isolated):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.repo = make_repo(cls.root / "code", "app")

    def test_scan_repo_matches_author_case_insensitively(self) -> None:
        commits = git.scan_repo(str(self.repo), ["me@example.com"], git.DEFAULT_IGNORE)
        authored = [c for c in commits if not c.get("co")]
        self.assertEqual(len(authored), 2)
        self.assertNotIn("Someone else's work", {c["s"] for c in commits})

    def test_scan_repo_counts_co_authored_commits_by_trailer_only(self) -> None:
        commits = git.scan_repo(str(self.repo), ["me@example.com"], git.DEFAULT_IGNORE)
        co = [c for c in commits if c.get("co")]
        self.assertEqual([c["s"] for c in co], ["Pair on the parser"])
        self.assertEqual(co[0]["e"], "them@example.com")  # the author stays the author
        # Naming your email in a message is not co-authorship.
        self.assertNotIn("Credit an idea", {c["s"] for c in commits})

    def test_scan_repo_skips_lockfile_lines_and_follows_renames(self) -> None:
        first, second = sorted(
            (
                c
                for c in git.scan_repo(str(self.repo), ["me@example.com"], git.DEFAULT_IGNORE)
                if not c.get("co")
            ),
            key=lambda c: c["t"],
        )
        self.assertEqual((first["a"], first["f"]), (10, 2))  # lockfile counted as a file only
        self.assertEqual(first["l"], {"Python": 10})
        # Seen as a rename of one edited line, not a 10-line delete plus a 10-line add.
        self.assertEqual((second["a"], second["d"], second["l"]), (1, 1, {"Python": 2}))
        everything = git.scan_repo(str(self.repo), ["me@example.com"], ())
        self.assertEqual(min(everything, key=lambda c: c["t"])["a"], 510)

    def test_collect_local_folders(self) -> None:
        data = collect_mod.collect(collect_args(local_root=[str(self.root / "code")]), "git", None)
        self.assertEqual(data["schema"], common.SCHEMA_VERSION)
        self.assertEqual([p["path"] for p in data["projects"]], ["team/app"])
        self.assertEqual(len(data["commits"]), 3)  # two authored, one co-authored
        self.assertEqual(data["user"]["name"], "me")
        self.assertEqual(data["source"]["provider"], "git")

    def test_rebased_and_cherry_picked_copies_count_once(self) -> None:
        repo = make_repo(self.root / "dupes", "dupes")
        env = {
            **os.environ,
            "GIT_COMMITTER_NAME": "Bot",
            "GIT_COMMITTER_EMAIL": "bot@example.com",
            "GIT_COMMITTER_DATE": "2025-01-01T00:00:00+00:00",
        }

        def run(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, env=env
            )
            return result.stdout.decode().strip()

        # Copy "Move app into src" onto a second branch: a new id, but the same author,
        # author time and message, exactly as a cherry-pick or a rebase leaves it.
        original = run("log", "--format=%H", "--grep=Move app into src")
        run("checkout", "-q", "-b", "copy", run("rev-list", "--max-parents=0", "HEAD"))
        run("cherry-pick", original)
        self.assertNotEqual(run("rev-parse", "HEAD"), original)

        args = collect_args(local_root=[str(self.root / "dupes")])
        subjects = [c["s"] for c in collect_mod.collect(args, "git", None)["commits"]]
        self.assertEqual(subjects.count("Move app into src"), 1)

    def test_coverage_says_how_each_repository_was_read(self) -> None:
        data = collect_mod.collect(collect_args(local_root=[str(self.root / "code")]), "git", None)
        cov = data["coverage"]
        self.assertEqual(cov["scanned"], 1)
        self.assertEqual(
            cov["repos"], [{"path": "team/app", "via": "local", "commits": 3, "fetchedAt": None}]
        )  # a checkout that was never fetched says so
        self.assertIn("No hosting API was used", cov["limits"][0])

    def test_last_fetch_reads_fetch_head(self) -> None:
        self.assertIsNone(git.last_fetch(str(self.repo)))
        head = self.repo / ".git" / "FETCH_HEAD"
        head.write_text("", encoding="utf-8")
        self.addCleanup(head.unlink)
        self.assertRegex(git.last_fetch(str(self.repo)), r"^\d{4}-\d\d-\d\dT")

    def test_excluded_emails_stay_out(self) -> None:
        args = collect_args(
            local_root=[str(self.root / "code")],
            email=["me@example.com", "them@example.com"],
            exclude_email=["them@example.com"],
        )
        data = collect_mod.collect(args, "git", None)
        self.assertEqual(data["source"]["emails"], ["me@example.com"])
        self.assertEqual(data["coverage"]["excludedEmails"], ["them@example.com"])
        self.assertNotIn("them@example.com", {c["e"] for c in data["commits"] if not c.get("co")})

    def test_collect_needs_something_to_read(self) -> None:
        with self.assertRaises(common.CollectError):
            collect_mod.collect(collect_args(), "git", None)

    def test_restricted_collect_refuses_non_https_and_unlisted_hosts(self) -> None:
        for url in ("file:///etc", "git@github.com:a/b.git", "https://internal.corp/a/b.git"):
            args = collect_args(repo=[url], allowed_hosts={"github.com"})
            with self.assertRaises(common.CollectError, msg=url):
                collect_mod.collect(args, "git", None)

    def test_restricted_collect_refuses_unlisted_api_host(self) -> None:
        args = collect_args(url="https://169.254.169.254", allowed_hosts={"gitlab.com"})
        with self.assertRaises(common.CollectError):
            collect_mod.collect(args, "gitlab", "token")

    def test_detect_identity_finds_every_email_under_your_name(self) -> None:
        subprocess.run(["git", "config", "--global", "user.name", "Someone"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "me@example.com"], check=True)
        self.addCleanup(lambda: (self.root / ".gitconfig").unlink(missing_ok=True))
        repos = git.discover_local([str(self.root / "code")], 4)
        found = detect.detect_identity(repos)
        self.assertEqual(found["name"], "Someone")
        # make_repo commits as "Someone" with two addresses; both are found, case-folded.
        self.assertEqual(
            {e["email"]: e["commits"] for e in found["emails"]},
            {"me@example.com": 2, "them@example.com": 3},
        )

    def test_suggest_url_reads_instance_from_remotes(self) -> None:
        repos = [
            {"host": "gitlab.corp.example"},
            {"host": "gitlab.corp.example"},
            {"host": "github.com"},
        ]
        self.assertEqual(detect.suggest_url("gitlab", repos), "https://gitlab.corp.example")
        self.assertEqual(detect.suggest_url("github", repos), "https://github.com")
        self.assertIsNone(detect.suggest_url("gitea", repos))

    def test_demo_and_site_render(self) -> None:
        out = self.root / "site"
        site.write_site(demo.make_demo(1, "gitlab"), out, False)
        page = (out / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "lib.js", "app.js", "data.js", "favicon.svg"):
            self.assertIn(f"{name}?v={assets.fingerprint(out / name)}", page)
        for name in common.WEB_FILES + ("data.js", "history.json"):
            self.assertTrue((out / name).exists(), name)
        data = json.loads((out / "history.json").read_text(encoding="utf-8"))
        self.assertGreater(len(data["commits"]), 100)


class ServerTests(Isolated):
    """Runs a real server on a free port, once hosted and once personal."""

    def server_args(self, host: str, **settings: object) -> argparse.Namespace:
        """`ghis serve`'s options with test-sized limits; keyword arguments override them."""
        base = dict(
            host=host,
            port=0,
            allowed_hosts=None,
            max_jobs=1,
            ttl=5,
            jobs=1,
            workdir=str(self.root),
            max_stories=3,
            git_timeout=60,
            public_url=None,
            default_provider=None,
            default_url=None,
        )
        base.update(settings)
        return argparse.Namespace(**base)

    def start(self, host_mode: str, **settings: object) -> tuple[ThreadingHTTPServer, int]:
        app = server.GhisServer(self.server_args(host_mode, **settings))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def request(
        self,
        port: int,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, {"raw": raw}

    JSON = {"Content-Type": "application/json"}

    def test_hosted_config_and_pages(self) -> None:
        _, port = self.start("0.0.0.0")
        status, config = self.request(port, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(config["personal"])
        self.assertIn("github.com", config["allowedHosts"])
        self.assertEqual(self.request(port, "GET", "/")[0], 200)
        # Pages leave the server with fingerprinted assets, against the files it serves.
        html = self.request(port, "GET", "/")[1]["raw"].decode()
        self.assertIn(f"connect.js?v={assets.fingerprint(common.WEB_DIR / 'connect.js')}", html)
        self.assertEqual(self.request(port, "GET", "/../ghis.py")[0], 404)

    def test_post_requires_json_and_same_origin(self) -> None:
        _, port = self.start("0.0.0.0")
        body = json.dumps({"provider": "git"}).encode()
        self.assertEqual(
            self.request(port, "POST", "/api/jobs", body, {"Content-Type": "text/plain"})[0], 415
        )
        self.assertEqual(
            self.request(
                port, "POST", "/api/jobs", body, {**self.JSON, "Origin": "https://evil.example"}
            )[0],
            403,
        )

    def test_refused_upload_gets_its_answer_not_a_reset(self) -> None:
        # The client is still sending a large body when the server decides. Answering
        # without reading it made macOS reset the connection, hiding the 403.
        _, port = self.start("0.0.0.0")
        big = json.dumps({"schema": 1, "commits": [], "pad": "x" * 8_000_000}).encode()
        for headers, expected in (
            ({**self.JSON, "Origin": "https://evil.example"}, 403),
            ({"Content-Type": "text/plain"}, 415),
            (self.JSON, 404),
        ):
            with self.subTest(expected=expected):
                path = "/api/nowhere" if expected == 404 else "/api/upload"
                self.assertEqual(self.request(port, "POST", path, big, headers)[0], expected)

    def test_public_url_decides_same_origin_behind_a_proxy(self) -> None:
        _, port = self.start("0.0.0.0", public_url="https://Stories.example.com/")
        story = json.dumps(demo.make_demo(2, "github")).encode()
        # A proxy that rewrites Host to its upstream name: the public URL still matches.
        proxied = {**self.JSON, "Host": "ghis:8080"}
        for origin, expected in (
            ("https://stories.example.com", 201),
            ("http://stories.example.com", 403),  # same name, other scheme
            ("https://stories.example.com.evil.example", 403),
            ("https://evil.example", 403),
        ):
            with self.subTest(origin=origin):
                status = self.request(
                    port, "POST", "/api/upload", story, {**proxied, "Origin": origin}
                )[0]
                self.assertEqual(status, expected)

    def test_form_defaults_reach_the_page(self) -> None:
        _, port = self.start(
            "0.0.0.0",
            allowed_hosts="gitlab.example.com",
            default_provider="gitlab",
            default_url="https://gitlab.example.com/",
        )
        config = self.request(port, "GET", "/api/config")[1]
        self.assertEqual(
            config["defaults"], {"provider": "gitlab", "url": "https://gitlab.example.com"}
        )

    def test_bad_settings_stop_the_server_with_a_reason(self) -> None:
        for settings, reason in (
            ({"public_url": "stories.example.com"}, "public URL"),
            ({"default_provider": "svn"}, "Unknown default provider"),
            ({"default_url": "https://gitlab.example.com"}, "needs a default provider"),
            ({"default_provider": "git", "default_url": "https://a.example"}, "needs a default"),
            (
                {"default_provider": "gitlab", "default_url": "https://gitlab.example.com"},
                "not in the allowed hosts",
            ),
        ):
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(common.CollectError, reason):
                    server.GhisServer(self.server_args("0.0.0.0", **settings))

    def test_hosted_job_refuses_unlisted_host(self) -> None:
        _, port = self.start("0.0.0.0")
        body = json.dumps(
            {"provider": "gitlab", "url": "http://169.254.169.254", "token": "x"}
        ).encode()
        status, started = self.request(port, "POST", "/api/jobs", body, self.JSON)
        self.assertEqual(status, 202)
        for _ in range(50):
            status, job = self.request(port, "GET", f"/api/jobs/{started['id']}")
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "failed")
        self.assertIn("not allowed", job["error"])

    def test_upload_validates_and_caps_storage(self) -> None:
        _, port = self.start("0.0.0.0")
        self.assertEqual(self.request(port, "POST", "/api/upload", b'{"x": 1}', self.JSON)[0], 400)
        story = json.dumps(demo.make_demo(2, "github")).encode()
        ids = []
        for _ in range(3):
            status, body = self.request(port, "POST", "/api/upload", story, self.JSON)
            self.assertEqual(status, 201)
            ids.append(body["id"])
        self.assertEqual(self.request(port, "POST", "/api/upload", story, self.JSON)[0], 400)
        status, data = self.request(port, "GET", f"/api/jobs/{ids[0]}/result")
        self.assertEqual((status, data["schema"]), (200, common.SCHEMA_VERSION))
        self.assertEqual(self.request(port, "GET", "/api/jobs/doesnotexist123")[0], 404)

    def test_personal_mode_adds_typed_emails_and_drops_unticked_ones(self) -> None:
        subprocess.run(["git", "config", "--global", "user.name", "Someone"], check=True)
        self.addCleanup(lambda: (self.root / ".gitconfig").unlink(missing_ok=True))
        make_repo(self.root / "code", "app")
        app = server.GhisServer(self.server_args("127.0.0.1"))
        options = argparse.Namespace(
            email=["extra@example.com"],
            exclude_email=["them@example.com"],
            local_root=[str(self.root / "code")],
            url=None,
        )
        app.fill_in(server.Job(), "git", options, None, [])
        # Typed emails join the detected ones (they used to replace them); unticked stay out.
        self.assertEqual(options.email, ["extra@example.com", "me@example.com"])

    def test_personal_mode_rejects_foreign_host_header(self) -> None:
        _, port = self.start("127.0.0.1")
        status, config = self.request(port, "GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["personal"])
        # A DNS-rebinding page reaches the same socket under its own name.
        self.assertEqual(
            self.request(port, "GET", "/api/config", None, {"Host": "attacker.example:8080"})[0],
            403,
        )
        for name in ("localhost:8080", "ghis.localhost:8080", "[::1]:8080"):
            self.assertEqual(self.request(port, "GET", "/api/config", None, {"Host": name})[0], 200)
        # Look-alikes that DNS could point anywhere stay out.
        for name in ("localhost.attacker.example", "ghislocalhost:8080", "evil.example"):
            self.assertEqual(self.request(port, "GET", "/api/config", None, {"Host": name})[0], 403)


class CliTests(Isolated):
    """The commands as people run them: through ghis.py, in a separate process."""

    LAUNCHER = str(Path(__file__).resolve().parent.parent / "ghis.py")

    def ghis(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.LAUNCHER, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "GHIS_HOME": str(self.root)},
        )

    def test_version_and_demo(self) -> None:
        self.assertRegex(self.ghis("--version").stdout, r"^ghis \d+\.\d+\.\d+")
        out = self.root / "story"
        self.assertEqual(self.ghis("demo", "--out", str(out)).returncode, 0)
        self.assertTrue((out / "index.html").exists())

    def test_render_rebuilds_from_history(self) -> None:
        out = self.root / "rerender"
        self.ghis("demo", "--out", str(out))
        (out / "app.js").unlink()
        self.assertEqual(self.ghis("render", "--out", str(out)).returncode, 0)
        self.assertTrue((out / "app.js").exists())

    def test_serve_starts_and_answers(self) -> None:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, self.LAUNCHER, "serve", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "GHIS_HOME": str(self.root)},
        )
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        for _ in range(50):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/healthz")
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
                return
            except OSError:
                if proc.poll() is not None:
                    self.fail(f"ghis serve exited: {proc.stdout.read()}")
                time.sleep(0.1)
        self.fail("ghis serve did not answer")

    def test_collect_reports_errors_without_a_traceback(self) -> None:
        result = self.ghis("collect", "--provider", "git")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Nothing to read from", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class CaFileTests(unittest.TestCase):
    PEM = "-----BEGIN CERTIFICATE-----\nTUlJQ0lUWQ==\n-----END CERTIFICATE-----\n"

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        # trust_ca_file points the whole process at its bundle; undo that after each test.
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(setattr, common, "EXTRA_CA", None)

    def test_extra_ca_is_added_to_the_system_ones(self) -> None:
        system = self.dir / "system.pem"
        system.write_text(self.PEM.replace("TUlJQ0lUWQ==", "U1lTVEVN"), encoding="ascii")
        extra = self.dir / "company.pem"
        extra.write_text(self.PEM, encoding="ascii")
        os.environ["SSL_CERT_FILE"] = str(system)
        bundle = common.trust_ca_file(str(extra))
        self.addCleanup(bundle.unlink)
        text = bundle.read_text(encoding="utf-8")
        self.assertIn("U1lTVEVN", text)  # the system's certificates are kept...
        self.assertIn("TUlJQ0lUWQ==", text)  # ...and the company's added
        self.assertEqual(os.environ["GIT_SSL_CAINFO"], str(bundle))  # for git
        self.assertEqual(common.EXTRA_CA, extra)  # for API calls, on top of the system's

    def test_a_file_without_certificates_is_refused(self) -> None:
        wrong = self.dir / "key.txt"
        wrong.write_text("not a certificate", encoding="ascii")
        with self.assertRaisesRegex(common.CollectError, "no PEM certificate"):
            common.trust_ca_file(str(wrong))
        with self.assertRaisesRegex(common.CollectError, "Cannot read"):
            common.trust_ca_file(str(self.dir / "missing.pem"))


if __name__ == "__main__":
    unittest.main()
