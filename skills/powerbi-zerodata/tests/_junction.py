"""Shared Windows junction helper for offline tests. Not a suite."""
from __future__ import annotations

import subprocess

_SKIP = "creating a junction is unavailable"


def make_junction(tc, link: str, target: str) -> bool:
    """Create a directory junction. OSError or a nonzero mklink skips; it does not fail the test.

    `tc` is a unittest.TestCase (skipTest) or None. None returns False so a script-style
    suite can print SKIP itself. A product assertion must never call this to skip.
    """
    try:
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    except OSError:
        if tc is not None and hasattr(tc, "skipTest"):
            tc.skipTest(_SKIP)
        return False
    if made.returncode != 0:
        if tc is not None and hasattr(tc, "skipTest"):
            tc.skipTest(_SKIP)
        return False
    return True
