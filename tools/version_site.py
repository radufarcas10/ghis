#!/usr/bin/env python3
"""Fingerprints every local file reference in a built website (the Pages workflow's _site):
pages, the 404 page, the web manifest and every demo story, so a deploy can never pair new
HTML with a stale cached script, stylesheet or icon.

    python3 tools/version_site.py _site --site-url https://radufarcas10.github.io/ghis/
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ghis.assets import version_tree  # noqa: E402

parser = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
)
parser.add_argument("root", type=Path, help="the built site")
parser.add_argument(
    "--site-url", help="the site's own address: absolute links under it are local files"
)
args = parser.parse_args()
count = version_tree(args.root.resolve(), args.site_url)
print(f"fingerprinted references in {count} files under {args.root}")
