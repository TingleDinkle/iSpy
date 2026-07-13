"""Shared test setup.

Environment variables are pinned BEFORE any tracker import so the test run
never depends on (or touches) the real API key or database: real env vars
take precedence over .env in pydantic-settings.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("APPSTORESPY_API_KEY", "test-key-not-real")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://tracker:tracker@localhost:5432/appstore_tracker_test",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
