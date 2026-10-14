"""ghis: your git history, told beautifully."""

import sys

# Older interpreters stop here with a sentence instead of a syntax error further in.
if sys.version_info < (3, 9):  # noqa: UP036
    sys.exit("ghis needs Python 3.9 or newer.")

__version__ = "0.1.0"
