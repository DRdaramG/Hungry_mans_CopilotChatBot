"""
Hungry Man's Copilot ChatBot — entry point.

Run with:
    python main.py
"""

import logging
import sys

# Require Python 3.10+ for the union-type hints used throughout the package.
if sys.version_info < (3, 10):
    sys.exit(
        "Python 3.10 or later is required.\n"
        f"You are running Python {sys.version_info.major}.{sys.version_info.minor}."
    )

# ---------------------------------------------------------------------------
# Debug logging — prints detailed auth & API info to the console.
# Set level to logging.WARNING to silence debug output once everything works.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)

from src.app import CopilotChatApp  # noqa: E402


def main() -> None:
    app = CopilotChatApp()
    app.run()


if __name__ == "__main__":
    main()
