# Contributing to ghis

Thanks for helping. ghis deliberately has **no runtime dependencies**: the Python standard
library, the `git` CLI and plain browser JavaScript. Please keep it that way, because it is
what makes `python3 ghis.py` work on any machine with nothing to install.

## Setup

```bash
git clone <this repo> && cd ghis
python3 ghis.py demo --open     # sample story, no account needed
python3 ghis.py                  # the app, on http://ghis.localhost:8080 (or the next free port)
```

Requirements: Python 3.9+ and git 2.31+. The development tools below are optional extras.

## Tests and checks

CI runs all of these on every push ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

| What | Command | Needs |
|---|---|---|
| Python tests | `python3 -m unittest discover tests` | nothing |
| JavaScript tests | `node --test tests/js/*.test.js` | Node 22+; the phone-layout test also needs Chrome and Python |
| Browser tests | included in the Python tests | Chrome or Chromium, else skipped (`GHIS_CHROME=/path` to point at one) |
| Lint and format | `ruff check . && ruff format --check .` | `pipx install ruff` |
| Docker image | `docker build -t ghis . && docker run -p 8080:8080 ghis` | Docker |

What each layer covers:

- **`tests/test_ghis.py`**: git scanning on real throwaway repositories, detection, the
  collector, and the web server's security checks (host allowlist, DNS-rebinding and
  cross-site guards, upload validation, storage caps).
- **`tests/test_providers.py`**: GitHub, GitLab and Gitea collectors against a fake host that
  replays their API responses, including pagination, deleted projects, rejected tokens and
  cross-host redirects. Nothing touches the network.
- **`tests/js/lib.test.js`**: the story page's logic in `web/lib.js`, such as stats, streaks,
  tickets, chart scales, escaping and safe links.
- **`tests/js/mobile.test.js`**: the story, the website and the app's landing page under real
  iPhone emulation (the DevTools protocol, as in DevTools' device mode). It fails if anything
  makes a page wider than the phone, because phones answer that by zooming the whole page out.
  Pinned-size frames and narrow desktop windows don't reproduce it, so check layouts this way.
- **`tests/test_browser.py`**: the real pages in headless Chrome. The offline story, the app
  through the server, and a hostile `history.json` that must stay inert.

## Layout

```
ghis.py                     launcher: runs src/ghis from a checkout, no install
src/ghis/
  cli.py                    the command line (collect, render, demo, serve)
  collect.py                gathers everything into the history.json shape
  providers/                github.py, gitlab.py, gitea.py, plus the registry in __init__.py
  git.py                    finding checkouts, cloning, reading commits
  detect.py                 personal mode: emails, instance and token already on this computer
  server.py                 the web app (personal and hosted modes)
  api.py, common.py         HTTP client; paths, logging, host checks
  site.py, demo.py          writing the static story; sample data
  web/                      the pages (shipped inside the package)
    lib.js                  story logic, no DOM: tested under Node, loaded by the page
    app.js                  story rendering, charts and motion
    connect.*               the app's landing page and progress view
    style.css, theme.js     shared styles and the theme switch
site/                       the project website (GitHub Pages)
tests/                      Python tests; tests/js/ for JavaScript
```

`history.json` is the contract between the collector and the page. Its `schema` number goes
up whenever a change would break older files.

**Why `lib.js` is a classic script and not an ES module:** the story page has to open
straight from disk (`out/index.html`), and browsers block module scripts on `file://`. The
small wrapper at the top of `lib.js` lets the same file load in the browser and in Node.

## The website

`site/` is the project website. [`.github/workflows/pages.yml`](.github/workflows/pages.yml)
publishes it on every push to `main`, together with three live demo stories built from
sample data: `demo/` (GitHub), `demo/gitlab/` and `demo/codeberg/`. To preview it locally:

```bash
mkdir -p _site && cp -r site/. _site/ && cp src/ghis/web/theme.js src/ghis/web/favicon.svg _site/
python3 ghis.py demo --out _site/demo
python3 -m http.server -d _site 8000     # http://localhost:8000
```

Repository links on the site fill themselves in from the `<owner>.github.io/<repo>` address,
so nothing needs editing after a fork or rename.

## Adding a provider

1. Add `src/ghis/providers/<name>.py` with `collect_<name>(url, token, args, warnings)`.
   Return the same dict the others do: `user`, `emails`, `projects` (with `sshUrl` /
   `httpUrl`), `requests`, `reviews`, `activity` (`{day: {action: count}}`, using the action
   names in `actionLabels()` in `web/lib.js`), `has` and `cloneUser`.
2. Register it in `providers/__init__.py` (`PROVIDERS` and `COLLECTORS`). Teach
   `detect.py` how to recognise its hosts and tokens.
3. In `web/connect.js`, add its icon, short label, token link (`TOKEN_HELP`) and
   step-by-step help (`TOKEN_STEPS`).
4. Add a replayed-API test to `tests/test_providers.py`, and a row to the provider table
   in `README.md`.

## Style

- Python: ruff's formatter and lint rules (see `pyproject.toml`), type hints throughout.
  Comment the *why*, not the *what*.
- JavaScript and CSS: tabs. Escape every value from data with `esc()` before it reaches
  HTML, and pass every link from data through `safeUrl()`. Logic that doesn't need the DOM
  belongs in `lib.js`, with a test.
- Charts: keep the categorical colour order in `style.css`, which was checked for
  colour-blind readers in both themes. Every chart needs a hover tooltip and a
  "View as table" fallback.
- Anything that reaches the network or the filesystem from the server must respect hosted
  mode: read [SECURITY.md](SECURITY.md) before touching `server.py`, `collect.py` or
  `git.clone_env()`.

## Pull requests

Keep them focused. Add a test for behaviour changes. For UI changes, include a screenshot
in light and dark.
