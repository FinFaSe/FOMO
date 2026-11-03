"""Real-network smoke test (no synthetic data). Run from backend/: `uv run python ../scripts/live_smoke.py`.

Same as `python -m tracker.smoke`; see backend/tracker/smoke.py for the checks and options.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from tracker.smoke import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
