"""Writes the static story page: the web assets plus the data."""

from __future__ import annotations

import json
import shutil
import webbrowser
from pathlib import Path
from typing import Any

from .common import WEB_DIR, WEB_FILES, log


def write_site(data: dict[str, Any], out: Path, open_browser: bool) -> None:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    for name in WEB_FILES:
        shutil.copy2(WEB_DIR / name, out / name)
    payload = json.dumps(data, separators=(",", ":"))
    (out / "history.json").write_text(payload, encoding="utf-8")
    # A script rather than fetch(), because browsers block fetch() on file:// pages.
    (out / "data.js").write_text(f"window.GHIS_DATA = {payload};\n", encoding="utf-8")
    page = out / "index.html"
    log(f"Done. Open {page}")
    if open_browser:
        webbrowser.open(page.as_uri())
