"""Filesystem locations shared by the application."""

from pathlib import Path


def project_root() -> Path:
    """Return the repository root (the directory that contains ``src/``).

    Returns:
        Absolute path of the repository root.
    """
    return Path(__file__).resolve().parents[2]
