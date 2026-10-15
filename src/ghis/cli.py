"""ghis - turn your git history into an animated web page.

Subcommands:
  collect  gather history from a hosting API and/or git repositories, then render
  render   re-render the page from an existing history.json (after editing web/)
  demo     render the page from generated sample data
  serve    run the web app: a connect page, live progress and hosted stories

Run with no subcommand to open the app in your browser (same as `serve --open`).

Providers (the API adds pull/merge requests, reviews and activity to the commits):
  github   GitHub.com or Enterprise Server                        (GITHUB_TOKEN)
  gitlab   GitLab.com or self-managed                             (GITLAB_TOKEN)
  gitea    Codeberg, Forgejo or Gitea                             (GITEA_TOKEN)
  git      no API, any host: commits from --repo URLs and/or --local-root checkouts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .collect import collect, pick_provider, read_token
from .common import DATA_DIR, CollectError, die, log, tilde, trust_ca_file
from .demo import make_demo
from .detect import find_token
from .providers import COLLECTORS, PROVIDERS
from .server import DEFAULT_ALLOWED_HOSTS
from .server import serve as run_server
from .site import write_site


def keep_clones(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--keep-clones",
        action="store_true",
        default=os.environ.get("GHIS_KEEP_CLONES", "") not in ("", "0"),
        help=f"keep downloaded repositories in {tilde(DATA_DIR / 'cache')} so the next story "
        "only fetches what changed (env GHIS_KEEP_CLONES=1; default: delete them after "
        "reading; never when hosted)",
    )


def ca_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ca-file",
        default=os.environ.get("GHIS_CA_FILE") or None,
        help="PEM file of extra certificate authorities to trust, such as your company's "
        "internal CA (env GHIS_CA_FILE)",
    )


def cmd_collect(args: argparse.Namespace) -> None:
    provider = pick_provider(args)
    token = read_token(args, provider)
    if provider in COLLECTORS and not token:
        url = (args.url or PROVIDERS[provider]["url"]).rstrip("/")
        found = find_token(provider, url)
        if found:
            token = found[0]
            log(f"Using your token from {found[1]} (@{found[2]}).")
    if args.keep_clones and not args.cache:
        args.cache = str(DATA_DIR / "cache")
    try:
        data = collect(args, provider, token)
    except CollectError as err:
        log(str(err))
        sys.exit(1)
    write_site(data, Path(args.out), args.open)


def cmd_render(args: argparse.Namespace) -> None:
    source = Path(args.input or Path(args.out) / "history.json").expanduser()
    if not source.exists():
        die(f"{source} not found; run `collect` or `demo` first.")
    write_site(json.loads(source.read_text(encoding="utf-8")), Path(args.out), args.open)


def cmd_demo(args: argparse.Namespace) -> None:
    write_site(make_demo(args.seed, args.provider), Path(args.out), args.open)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # an odd character never crashes ghis
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="ghis", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"ghis {__version__}")
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", default=str(DATA_DIR / "out"), help="output folder (default: out/)")
        p.add_argument("--open", action="store_true", help="open the page when done")

    collect = sub.add_parser("collect", help="gather history and render the page")
    common(collect)
    collect.add_argument(
        "--provider",
        choices=tuple(PROVIDERS),
        help="API to read; default: whichever of GITHUB_TOKEN / GITLAB_TOKEN "
        "/ GITEA_TOKEN is set, else git",
    )
    collect.add_argument(
        "--url",
        help="instance base URL for self-hosted GitLab, GitHub "
        "Enterprise, Forgejo or Gitea (default: github.com, "
        "gitlab.com, codeberg.org)",
    )
    collect.add_argument("--token-file", help="read the token from this file")
    collect.add_argument(
        "--repo",
        action="append",
        default=[],
        help="clone URL of a repository on any host; repeatable",
    )
    collect.add_argument(
        "--clone-user",
        default="git",
        help="HTTPS username sent with the token for --repo URLs "
        "(default: git; Bitbucket wants your username)",
    )
    collect.add_argument("--name", help="display name when no API supplies one")
    collect.add_argument(
        "--email", action="append", default=[], help="extra author email to match; repeatable"
    )
    collect.add_argument(
        "--exclude-email",
        action="append",
        default=[],
        help="an email that is not yours, to leave out (e.g. a teammate with your name); "
        "repeatable",
    )
    collect.add_argument(
        "--local-root",
        action="append",
        default=[],
        help="folder to scan for existing checkouts; repeatable",
    )
    collect.add_argument("--depth", type=int, default=4, help="how deep to scan --local-root")
    collect.add_argument(
        "--no-clone",
        action="store_true",
        help="never clone; read commits from local checkouts only",
    )
    collect.add_argument("--clone-via", choices=("ssh", "https"), default="ssh")
    keep_clones(collect)
    collect.add_argument(
        "--cache", default="", help="keep clones in this folder instead (implies --keep-clones)"
    )
    collect.add_argument(
        "--all-member-projects",
        action="store_true",
        help="also clone every repository you are a member of",
    )
    collect.add_argument(
        "--count-all-files",
        action="store_true",
        help="count lockfile, build and vendored lines too",
    )
    collect.add_argument("--jobs", type=int, default=4, help="parallel git processes")
    collect.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ca_file(collect)
    collect.set_defaults(func=cmd_collect)

    render = sub.add_parser("render", help="re-render from an existing history.json")
    common(render)
    render.add_argument("--input", help="history.json to read (default: <out>/history.json)")
    render.set_defaults(func=cmd_render)

    demo = sub.add_parser("demo", help="render the page from generated sample data")
    common(demo)
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument(
        "--provider",
        choices=tuple(COLLECTORS),
        default="github",
        help="which vocabulary the sample uses",
    )
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="run the ghis web app")
    serve.add_argument("--host", default=os.environ.get("GHIS_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ["PORT"]) if os.environ.get("PORT") else None,
        help="port to listen on (env PORT; default 8080, or the next free one "
        "when running on this computer)",
    )
    serve.add_argument(
        "--allowed-hosts",
        help="comma-separated hosts the server may call, or * for any "
        f"(env GHIS_ALLOWED_HOSTS; default {DEFAULT_ALLOWED_HOSTS})",
    )
    serve.add_argument(
        "--max-jobs",
        type=int,
        default=int(os.environ.get("GHIS_MAX_JOBS", "2")),
        help="collections running at once; more wait in line",
    )
    serve.add_argument(
        "--ttl",
        type=int,
        default=int(os.environ.get("GHIS_TTL_MINUTES", "0")),
        help="minutes a finished story stays available (default: 60, or a day in personal mode)",
    )
    serve.add_argument("--open", action="store_true", help="open the app in your browser")
    serve.add_argument(
        "--max-stories",
        type=int,
        default=int(os.environ.get("GHIS_MAX_STORIES", "200")),
        help="stories held in memory at once; new ones are refused beyond it",
    )
    serve.add_argument(
        "--git-timeout",
        type=int,
        default=int(os.environ.get("GHIS_GIT_TIMEOUT", "600")),
        help="seconds one clone or log may take when hosted",
    )
    serve.add_argument(
        "--public-url",
        default=os.environ.get("GHIS_PUBLIC_URL"),
        help="the address people open, e.g. https://stories.example.com (env GHIS_PUBLIC_URL); "
        "needed behind a proxy that rewrites the Host header",
    )
    serve.add_argument(
        "--default-provider",
        choices=list(PROVIDERS),
        default=os.environ.get("GHIS_DEFAULT_PROVIDER") or None,
        help="provider the form starts on (env GHIS_DEFAULT_PROVIDER)",
    )
    serve.add_argument(
        "--default-url",
        default=os.environ.get("GHIS_DEFAULT_URL") or None,
        help="instance the form starts with, e.g. https://gitlab.example.com "
        "(env GHIS_DEFAULT_URL; needs --default-provider)",
    )
    serve.add_argument("--jobs", type=int, default=4, help="parallel git processes per job")
    keep_clones(serve)
    ca_file(serve)
    serve.add_argument("--workdir", help="where temporary clones go (default: system temp)")
    serve.set_defaults(func=run_server)

    args = parser.parse_args()
    if not args.command:
        # Plain `python3 ghis.py`: the friendly way in, the app on this computer.
        args = parser.parse_args(["serve", "--open"])
    try:
        if getattr(args, "ca_file", None):
            trust_ca_file(args.ca_file)
        args.func(args)
    except CollectError as err:
        log(str(err))
        sys.exit(1)
