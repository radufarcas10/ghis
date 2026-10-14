#!/usr/bin/env python3
"""Run ghis straight from a checkout, no install needed: `python3 ghis.py`.

The code lives in src/ghis/. Installed with pip/pipx, the same thing is the `ghis` command.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ghis.cli import main  # noqa: E402

main()
