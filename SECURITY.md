# Security

ghis handles access tokens and private source code, so security reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private
[**Report a vulnerability**](../../security/advisories/new) form on this repository.
Include steps to reproduce and the mode you were running (CLI, personal server or hosted
server). You should get a reply within a week.

## What ghis promises

**Tokens**
- Used only to call the API of the host you chose and to clone from it.
- Never written to disk, never logged, never included in output files.
- Passed to git through the environment, never on a command line or in a git config.

**Hosted mode** (`ghis.py serve` bound to anything other than localhost, and the Docker image)
- Calls only the hosts in `GHIS_ALLOWED_HOSTS`, clones over `https://` only, never follows a
  git redirect, and follows API redirects only within the same host.
- Never reads the server's own filesystem: the local-folders feature is not available.
- Deletes each job's clones when the job ends. Keeps finished stories in memory only, for
  `GHIS_TTL_MINUTES`, and caps how many it holds (`GHIS_MAX_STORIES`).
- A story is readable by anyone who has its link. Links are 144-bit random, and job ids
  are redacted from access logs.
- Rejects cross-site POSTs (an `Origin` check plus a JSON-only body) and serves every page
  with a strict Content-Security-Policy.
- Limits each git command to `GHIS_GIT_TIMEOUT` seconds. Disk use is bounded by the
  temporary volume you give it.

**Personal mode** (`python3 ghis.py`, bound to localhost)
- Can read folders on your computer and clone with your SSH keys, so it answers only to
  requests addressed to `localhost`, `*.localhost` (such as `ghis.localhost`, which
  browsers always resolve to this computer), `127.0.0.1` or `[::1]`. That blocks
  DNS-rebinding pages, and the cross-site checks above stop other websites from starting jobs.

## Out of scope

- A hosted server run with `GHIS_ALLOWED_HOSTS=*` on a network that has internal services.
- Denial of service by an operator's own users beyond the limits above. Put a rate-limiting
  proxy in front of a public instance.
- Contents of a `history.json` you choose to share: it holds your commit messages and
  repository names.
