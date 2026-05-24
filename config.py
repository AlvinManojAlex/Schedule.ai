"""
config.py

Central configuration for Schedule.ai.
Edit TIMEZONE to match your local timezone.
All file paths are relative to the project root.
"""

import os
from pathlib import Path

# Load .env if present
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# User settings — edit these
# ---------------------------------------------------------------------------

TIMEZONE = "America/New_York"       # IANA timezone string

# ---------------------------------------------------------------------------
# Paths (no need to change these)
# ---------------------------------------------------------------------------

ROOT        = Path(__file__).parent
DATA_DIR    = ROOT / "data"
AUTH_DIR    = ROOT / "auth"

TASKS_PATH   = DATA_DIR / "tasks.json"
BLOCKED_PATH = DATA_DIR / "blocked.json"