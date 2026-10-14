"""Thin wrappers around the git CLI for change detection and the watch loop."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitError


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise GitError("git is not installed") from None
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_repo(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--short=12", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def changed_files(root: Path, ref: str) -> set[Path]:
    """Absolute paths changed between ``ref`` and the working tree, untracked files included."""
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.strip())
    _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    tracked = _git(root, "diff", "--name-only", "--no-renames", "-z", ref, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--full-name", "-z")
    names = [n for out in (tracked.stdout, untracked.stdout) for n in out.split("\0") if n]
    return {top / n for n in names}


def pull(root: Path) -> str:
    return _git(root, "pull", "--ff-only").stdout.strip()
