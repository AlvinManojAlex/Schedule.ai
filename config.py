"""
config.py

Central configuration for Schedule.ai.
Edit TIMEZONE to match your local timezone.
All file paths are relative to the project root.
"""

from pathlib import Path

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