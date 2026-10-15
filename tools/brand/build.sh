#!/usr/bin/env bash
# Rebuilds every image that contains the ghis mark, after a change to the logo (whose
# source of truth is src/ghis/web/favicon.svg; keep icon.html and og.html in step with it).
# Needs Chrome (or GHIS_CHROME) and Node 22+. Run from anywhere: tools/brand/build.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
chrome="${GHIS_CHROME:-}"
if [ -z "$chrome" ]; then
	for candidate in google-chrome google-chrome-stable chromium "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
		if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then chrome="$candidate"; break; fi
	done
fi
[ -n "$chrome" ] || { echo "Chrome not found: set GHIS_CHROME to its path" >&2; exit 1; }

shot() { # <page> <width> <height> <scale> <out>
	"$chrome" --headless=new --disable-gpu --no-sandbox --hide-scrollbars --window-size="$2,$3" \
		--force-device-scale-factor="$4" --screenshot="$5" "file://$here/$1" 2>/dev/null
	echo "saved ${5#"$root"/}"
}

# The icon page fills the window, so each size is a window of that size.
shot icon.html 512 512 1 "$root/site/icon-512.png"
shot icon.html 192 192 1 "$root/site/icon-192.png"
shot icon.html 180 180 1 "$root/site/apple-touch-icon.png"
shot og.html 1200 630 1 "$root/site/og.png"

# The README's share-card example, from a demo story's current year.
story="$(mktemp -d)"
python3 "$root/ghis.py" demo --provider github --out "$story" >/dev/null 2>&1
node "$here/share-card.mjs" "file://$story/index.html#$(date +%Y)" "$root/docs/share-card.png"
rm -rf "$story"
