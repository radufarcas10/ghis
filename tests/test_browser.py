"""End-to-end checks in headless Chrome: the pages load, run their JavaScript and render.
Skipped when no Chrome or Chromium is installed (set GHIS_CHROME to point at one)."""

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
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ghis import demo, server, site  # noqa: E402


def find_chrome() -> str | None:
    """Chrome or Chromium on Linux, macOS or Windows; GHIS_CHROME wins."""
    if os.environ.get("GHIS_CHROME"):
        return os.environ["GHIS_CHROME"]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if path := shutil.which(name):
            return path
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        *(
            str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
            for base in (
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            )
            if base
        ),
    ]
    return next((c for c in candidates if Path(c).is_file()), None)


CHROME = find_chrome()
SECTIONS = (
    "calendar",
    "months",
    "rhythm",
    "repos",
    "languages",
    "requests",
    "people",
    "highlights",
    "milestones",
    "words",
    "coverage",
    "share",
)


def render(url: str) -> str:
    """The page's DOM after its scripts ran (and its fetches settled)."""
    proc = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--force-prefers-reduced-motion",
            "--virtual-time-budget=5000",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    return proc.stdout


@unittest.skipUnless(CHROME, "no Chrome or Chromium found")
class BrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assert_story(self, dom: str) -> None:
        for section in SECTIONS:
            self.assertIn(f'id="{section}"', dom, f"section {section} did not render")
        self.assertIn('class="foot"', dom)
        self.assertNotIn("Story unavailable", dom)

    def test_offline_story_renders_every_section(self) -> None:
        site.write_site(demo.make_demo(3, "gitlab"), self.tmp, False)
        dom = render((self.tmp / "index.html").as_uri())
        self.assert_story(dom)
        self.assertIn("Ada Example", dom)
        self.assertIn("MRs merged", dom)  # GitLab vocabulary
        self.assertIn('class="pill"', dom)  # the year filter
        self.assertIn("in 1 min 14 s", dom)  # how long it took to build

    def test_hostile_data_is_inert(self) -> None:
        data = demo.make_demo(4, "github")
        data["commits"][-1]["s"] = '<img src=x onerror="document.title=1">'
        data["projects"][0]["webUrl"] = "javascript:alert(1)"
        data["requests"][0]["url"] = "javascript:alert(2)"
        data["source"]["buildSeconds"] = '<img src=x onerror="document.title=2">'  # not a number
        site.write_site(data, self.tmp, False)
        dom = render((self.tmp / "index.html").as_uri())
        self.assert_story(dom)
        self.assertNotIn("<img src", dom)
        self.assertNotIn('href="javascript:', dom)

    def test_app_pages_through_the_server(self) -> None:
        args = argparse.Namespace(
            host="0.0.0.0",
            port=0,
            allowed_hosts=None,
            max_jobs=1,
            ttl=5,
            jobs=1,
            workdir=str(self.tmp),
            max_stories=5,
            git_timeout=60,
            public_url=None,
            default_provider="gitlab",  # a team's server can start everyone on its host
            default_url=None,
            keep_clones=False,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.GhisServer(args)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        connect = render(base + "/")
        for provider in ("github", "gitlab", "gitea", "git"):
            self.assertIn(f'data-provider="{provider}"', connect)
        self.assertIn("How do I get a token?", connect)
        self.assertRegex(connect, r'data-provider="gitlab" aria-checked="true"')
        # Keeping downloads is this computer's owner's choice; a hosted form never offers it.
        self.assertRegex(connect, r'id="f-keep" hidden')

        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        body = json.dumps(demo.make_demo(5, "gitea"))
        conn.request("POST", "/api/upload", body, {"Content-Type": "application/json"})
        story_id = json.loads(conn.getresponse().read())["id"]
        conn.close()
        self.assert_story(render(f"{base}/story?job={story_id}"))
        self.assert_story(render(f"{base}/story?demo=github"))


if __name__ == "__main__":
    unittest.main()
