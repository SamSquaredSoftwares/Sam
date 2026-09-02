"""Make the action modules importable from the test suite."""

import sys
from pathlib import Path

ACTIONS_DIR = Path(__file__).parent / "actions"
if str(ACTIONS_DIR) not in sys.path:
    sys.path.insert(0, str(ACTIONS_DIR))
