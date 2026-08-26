"""Isolate the project virtualenv from the machine's base Python packages."""

from __future__ import annotations

import os
import sys


if sys.prefix != sys.base_prefix:
    base_site = os.path.normcase(
        os.path.abspath(os.path.join(sys.base_prefix, "Lib", "site-packages"))
    )
    sys.path[:] = [
        entry
        for entry in sys.path
        if not entry
        or os.path.normcase(os.path.abspath(entry)) != base_site
    ]
