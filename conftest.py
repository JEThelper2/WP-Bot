"""Root conftest: make track-b/tests/ importable (for wp_fake shared fixture)."""

import sys
from pathlib import Path

_track_b_tests = str(Path(__file__).parent / "track-b" / "tests")
if _track_b_tests not in sys.path:
    sys.path.insert(0, _track_b_tests)
