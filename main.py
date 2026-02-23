"""
Hungry Man's Copilot ChatBot — entry point.

Run with:
    python main.py
"""

import sys

# Require Python 3.10+ for the union-type hints used throughout the package.
if sys.version_info < (3, 10):
    sys.exit(
        "Python 3.10 or later is required.\n"
        f"You are running Python {sys.version_info.major}.{sys.version_info.minor}."
    )

from src.app import CopilotChatApp  # noqa: E402


def main() -> None:
    app = CopilotChatApp()
    app.run()


if __name__ == "__main__":
    main()
