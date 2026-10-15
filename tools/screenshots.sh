#!/usr/bin/env bash
# Rebuilds the README screenshots (docs/story.png, docs/app.png) from sample data only.
# The app screenshot runs ghis against a throwaway home folder holding two demo repositories,
# so the "what we found" panel shows Ada Example, never the real computer's repos or emails.
# Needs Chrome (or GHIS_CHROME). Run from anywhere: tools/screenshots.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
chrome="${GHIS_CHROME:-}"
if [ -z "$chrome" ]; then
	for candidate in google-chrome google-chrome-stable chromium "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
		if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then chrome="$candidate"; break; fi
	done
fi
[ -n "$chrome" ] || { echo "Chrome not found: set GHIS_CHROME to its path" >&2; exit 1; }
work="$(mktemp -d)"
trap 'kill "$server" 2>/dev/null || true; rm -rf "$work"' EXIT

shot() { # <url> <width> <height> <out>
	"$chrome" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --force-prefers-reduced-motion \
		--virtual-time-budget=8000 --window-size="$2,$3" --screenshot="$4" "$1" 2>/dev/null
	echo "saved ${4#"$root"/}"
}

# The story: the demo, all time, top of the page.
python3 "$root/ghis.py" demo --provider github --out "$work/story" >/dev/null 2>&1
shot "file://$work/story/index.html" 1280 2300 "$root/docs/story.png"

# A pretend computer: Ada's identity and two checkouts with GitHub remotes.
home="$work/home"
mkdir -p "$home/code"
export HOME="$home" GIT_CONFIG_NOSYSTEM=1 XDG_CONFIG_HOME="$home/.config"
git config --global user.name "Ada Example"
git config --global user.email "ada@example.com"
git config --global init.defaultBranch main
for repo in storefront-ui orders-api; do
	dir="$home/code/$repo"
	git init -q "$dir"
	git -C "$dir" remote add origin "git@github.com:ada/$repo.git"
	for i in 1 2 3; do
		echo "$i" > "$dir/file$i.txt"
		git -C "$dir" add -A
		GIT_AUTHOR_EMAIL=$([ "$i" = 3 ] && echo ada@work.example || echo ada@example.com) \
			git -C "$dir" commit -q -m "Change $i"
	done
done

# The app on that computer, in personal mode, with no token anywhere.
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
env -u GITHUB_TOKEN -u GITLAB_TOKEN -u GITEA_TOKEN -u GHIS_TOKEN GHIS_HOME="$home/ghis" \
	python3 "$root/ghis.py" serve --port "$port" >"$work/server.log" 2>&1 &
server=$!
for _ in $(seq 1 50); do curl -fs "http://localhost:$port/healthz" >/dev/null && break; sleep 0.2; done
shot "http://localhost:$port/" 1280 1750 "$root/docs/app.png"
