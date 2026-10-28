"""Repo and vendor paths. Puts the vendored CLI package on sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
VENDOR = ROOT / "vendor"
TEMPLATES = PACKAGE_DIR / "templates"
STATIC = PACKAGE_DIR / "static"

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))
