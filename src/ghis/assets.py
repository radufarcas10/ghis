"""Cache-busting: every reference to a local file carries a fingerprint of that file's contents
(style.css?v=3f9a1c02). The address changes exactly when the file does, so a browser never
pairs a new page with a stale cached script, stylesheet or icon, and nobody bumps a number.

Applied wherever pages leave ghis: a story written to disk (site.write_site), pages sent by the
server (server.py), and the website build (tools/version_site.py in the Pages workflow)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# href="…" / src="…" naming a file of these kinds; an earlier ?v= tag is replaced.
_ATTRIBUTE = re.compile(
    r'((?:href|src)=")([^"?#]+\.(?:css|js|mjs|svg|png|webmanifest|json))(?:\?v=[^"#]*)?(")'
)
# A web manifest's icons: "src": "icon-192.png".
_MANIFEST = re.compile(r'("src"\s*:\s*")([^"?#]+)(?:\?v=[^"#]*)?(")')


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def version_refs(
    text: str,
    base: Path,
    site_url: str | None = None,
    site_root: Path | None = None,
    manifest: bool = False,
) -> str:
    """Fingerprint the local files `text` refers to. Relative paths resolve against `base`
    (the referring file's folder); absolute URLs under `site_url` resolve against `site_root`.
    Anything else (other sites, API calls, files that do not exist) is left untouched."""

    def stamp(match: re.Match[str]) -> str:
        ref = match.group(2)
        if ref.startswith(("http://", "https://", "//")):
            if not (site_url and site_root and ref.startswith(site_url)):
                return match.group(0)
            target = site_root / ref[len(site_url) :]
        elif ref.startswith(("data:", "mailto:", "/")):
            return match.group(0)
        else:
            target = base / ref
        if not target.is_file():
            return match.group(0)
        return f"{match.group(1)}{ref}?v={fingerprint(target)}{match.group(3)}"

    return (_MANIFEST if manifest else _ATTRIBUTE).sub(stamp, text)


def version_file(path: Path, site_url: str | None = None, site_root: Path | None = None) -> None:
    """Fingerprint one HTML page or web manifest in place."""
    text = path.read_text(encoding="utf-8")
    versioned = version_refs(
        text, path.parent, site_url, site_root, manifest=path.suffix == ".webmanifest"
    )
    if versioned != text:
        path.write_text(versioned, encoding="utf-8")


def version_tree(root: Path, site_url: str | None = None) -> int:
    """Fingerprint every page and manifest under `root`; returns how many were looked at.
    Manifests go first: stamping their icons changes them, and pages fingerprint the result."""
    files = sorted(root.rglob("*.webmanifest")) + sorted(root.rglob("*.html"))
    for path in files:
        version_file(path, site_url, root)
    return len(files)
