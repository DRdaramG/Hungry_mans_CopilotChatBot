"""
Central path configuration for Hungry Man's Copilot ChatBot.

All persistent files are stored under the ``Asset/`` folder that lives
alongside ``main.py`` (i.e. the project root), regardless of the current
working directory when the application is launched.

Usage in other modules::

    from .paths import ASSET_DIR, asset_path
    MY_FILE = asset_path("my_file.json")
"""

import os

# Project root = the directory that contains main.py
# This file lives in src/, so we go one level up.
_PROJECT_ROOT: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Absolute path to the ``Asset/`` folder.  Created on first import.
ASSET_DIR: str = os.path.join(_PROJECT_ROOT, "Asset")
os.makedirs(ASSET_DIR, exist_ok=True)


def asset_path(filename: str) -> str:
    """Return the absolute path for *filename* inside the Asset folder."""
    return os.path.join(ASSET_DIR, filename)
