"""Helpers for loading sensitive settings without exposing them in Compose output."""

from __future__ import annotations

import os
from pathlib import Path


def env_secret(name: str, default: str = "") -> str:
    """Return NAME, or the contents of NAME_FILE when set.

    Docker Compose expands env_file values in `docker compose config`. For production
    secrets, set only NAME_FILE and mount the secret as a file.
    """
    file_name = os.environ.get(f"{name}_FILE", "")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)
