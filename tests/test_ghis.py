"""Tests for ghis. Standard library only: run with `python3 -m unittest discover tests`."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ghis import collect as collect_mod  # noqa: E402
from ghis import common, demo, detect, git, server, site  # noqa: E402


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
    (repo / "app.py").write_text("\n".join(lines) + "\n")
    (repo / "package-lock.json").write_text("{}\n" * 500)
    commit("APP-1 Add app", "me@example.com", "2024-03-04T10:00:00+02:00")
    (repo / "src").mkdir()
    (repo / "app.py").rename(repo / "src" / "main.py")
    lines[3] = "value_3 = 'changed'"
    (repo / "src" / "main.py").write_text("\n".join(lines) + "\n")
    commit("Move app into src", "ME@Example.com", "2024-03-05T23:30:00+02:00")
    (repo / "other.js").write_text("x\n")
    commit("Someone else's work", "them@example.com", "2024-03-06T09:00:00+02:00")
    return repo


class Isolated(unittest.TestCase):
    """Keeps the developer's own git config out of the tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.env = {
            "HOME": str(cls.root),
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


class CollectTests(Isolated):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.repo = make_repo(cls.root / "code", "app")

    def test_scan_repo_matches_author_case_insensitively(self) -> None:
        commits = git.scan_repo(str(self.repo), ["me@example.com"], git.DEFAULT_IGNORE)
        self.assertEqual(len(commits), 2)
        subjects = {c["s"] for c in commits}
        self.assertNotIn("Someone else's work", subjects)

    def test_scan_repo_skips_lockfile_lines_and_follows_renames(self) -> None:
        first, second = sorted(
            git.scan_repo(str(self.repo), ["me@example.com"], git.DEFAULT_IGNORE),
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
        self.assertEqual(len(data["commits"]), 2)
        self.assertEqual(data["user"]["name"], "me")
        self.assertEqual(data["source"]["provider"], "git")

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
            {"me@example.com": 2, "them@example.com": 1},
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
        for name in common.WEB_FILES + ("data.js", "history.json"):
            self.assertTrue((out / name).exists(), name)
        data = json.loads((out / "history.json").read_text())
        self.assertGreater(len(data["commits"]), 100)


class ServerTests(Isolated):
    """Runs a real server on a free port, once hosted and once personal."""

    def start(self, host_mode: str) -> tuple[ThreadingHTTPServer, int]:
        args = argparse.Namespace(
            host=host_mode,
            port=0,
            allowed_hosts=None,
            max_jobs=1,
            ttl=5,
            jobs=1,
            workdir=str(self.root),
            max_stories=3,
            git_timeout=60,
        )
        app = server.GhisServer(args)
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


if __name__ == "__main__":
    unittest.main()


class CliTests(Isolated):
    """The commands as people run them: through ghis.py, in a separate process."""

    LAUNCHER = str(Path(__file__).resolve().parent.parent / "ghis.py")

    def ghis(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.LAUNCHER, *args],
            capture_output=True,
            text=True,
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
