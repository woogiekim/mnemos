"""Thin git wrapper for mnemos sync operations.

All subprocess git calls in the entire mnemos codebase MUST go through this
module (Dependency Inversion Principle — #24 constraint).  No other file may
call ``subprocess.run(["git", ...])``.

Public API
----------
- :func:`init` — initialise a git repository
- :func:`set_remote` — add or update a named remote URL
- :func:`remote_exists` — check whether a named remote is present
- :func:`current_branch` — return the current branch name
- :func:`pull_rebase` — pull with rebase (and optional autostash)
- :func:`add` — stage files for commit
- :func:`commit` — create a commit (idempotent — no-ops on clean tree)
- :func:`push` — push to a remote branch
- :func:`status` — return ahead/behind/dirty information as a dict
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitNotFoundError(RuntimeError):
    """Raised when the ``git`` binary is not on PATH."""


class GitCommandError(RuntimeError):
    """Raised when a git sub-process exits with a non-zero return code.

    Attributes
    ----------
    returncode : int
        Exit code of the failed git command.
    stderr : str
        Captured stderr output.
    """

    def __init__(self, returncode: int, stderr: str, cmd: list[str] | None = None) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = cmd or []
        super().__init__(
            f"git command failed (rc={returncode}): {stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: str | Path) -> tuple[int, str, str]:
    """Run a git command and return ``(returncode, stdout, stderr)``.

    Raises
    ------
    GitNotFoundError
        When the ``git`` binary cannot be found on PATH.
    """
    if shutil.which("git") is None:
        raise GitNotFoundError(
            "git binary not found on PATH — install git before using mnemos sync"
        )
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init(path: str | Path) -> None:
    """Initialise a git repository at *path*.

    Idempotent — safe to call on an already-initialised repo.
    """
    rc, _, stderr = _git("init", cwd=path)
    if rc != 0:
        raise GitCommandError(rc, stderr, ["git", "init"])


def set_remote(path: str | Path, name: str, url: str) -> None:
    """Add or update the remote *name* to point at *url*.

    If the remote already exists with a different URL, it is updated via
    ``git remote set-url``.  If it does not exist yet, it is added via
    ``git remote add``.
    """
    # Check if the remote already exists
    rc, stdout, _ = _git("remote", "get-url", name, cwd=path)
    if rc == 0:
        # Remote exists — update the URL if it changed
        if stdout.strip() != url:
            rc2, _, stderr2 = _git("remote", "set-url", name, url, cwd=path)
            if rc2 != 0:
                raise GitCommandError(rc2, stderr2, ["git", "remote", "set-url", name, url])
    else:
        # Remote does not exist — add it
        rc3, _, stderr3 = _git("remote", "add", name, url, cwd=path)
        if rc3 != 0:
            raise GitCommandError(rc3, stderr3, ["git", "remote", "add", name, url])


def remote_exists(path: str | Path, name: str) -> bool:
    """Return ``True`` if a remote named *name* is configured in the repo."""
    rc, _, _ = _git("remote", "get-url", name, cwd=path)
    return rc == 0


def current_branch(path: str | Path) -> str:
    """Return the current branch name.

    Falls back to ``git rev-parse --abbrev-ref HEAD`` which works even on
    older git versions that do not have ``git branch --show-current``.
    """
    rc, stdout, stderr = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
    if rc != 0:
        raise GitCommandError(rc, stderr, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return stdout.strip()


def pull_rebase(
    path: str | Path,
    remote: str,
    branch: str,
    autostash: bool = True,
) -> None:
    """Pull from *remote*/*branch* with rebase.

    Parameters
    ----------
    path:
        Working-tree root (vault path).
    remote:
        Remote name (e.g. ``"origin"``).
    branch:
        Remote branch name (e.g. ``"main"``).
    autostash:
        When ``True``, pass ``--autostash`` so local dirty changes are
        stashed and re-applied around the rebase automatically.

    Raises
    ------
    GitCommandError
        On any non-zero exit (including unresolved merge conflicts).
    """
    cmd = ["pull", "--rebase"]
    if autostash:
        cmd.append("--autostash")
    cmd += [remote, branch]
    rc, _, stderr = _git(*cmd, cwd=path)
    if rc != 0:
        raise GitCommandError(rc, stderr, ["git"] + cmd)


def add(path: str | Path, files: Sequence[str]) -> None:
    """Stage *files* for the next commit.

    Parameters
    ----------
    path:
        Working-tree root.
    files:
        Sequence of file paths (relative to *path* or absolute).
    """
    if not files:
        return
    rc, _, stderr = _git("add", "--", *[str(f) for f in files], cwd=path)
    if rc != 0:
        raise GitCommandError(rc, stderr, ["git", "add", "--"] + [str(f) for f in files])


def commit(path: str | Path, message: str) -> bool:
    """Create a commit with *message*.

    This is idempotent: when the index is clean (nothing to commit) the call
    returns ``False`` without raising an error.

    Returns
    -------
    bool
        ``True`` if a commit was actually created, ``False`` when there was
        nothing to commit.

    Raises
    ------
    GitCommandError
        On unexpected git errors (the exit code ``1`` with "nothing to commit"
        stdout is treated as the clean-tree case and does NOT raise).
    """
    # Check whether there is anything staged to commit first.
    # ``git diff --cached --quiet`` exits 0 when the index is clean (nothing
    # staged), and 1 when there are staged changes ready to commit.
    rc_diff, _, _ = _git("diff", "--cached", "--quiet", cwd=path)
    if rc_diff == 0:
        # Nothing staged — also check for untracked that were added
        # via ``add .`` etc. by checking the full status.
        rc_status, out_status, _ = _git("status", "--porcelain", cwd=path)
        if rc_status != 0 or not any(
            not l.startswith("??") for l in out_status.splitlines() if l.strip()
        ):
            # Clean index — idempotent no-op
            return False

    rc, stdout, stderr = _git("commit", "-m", message, cwd=path)
    if rc == 0:
        return True
    # git exits with 1 when there is nothing to commit — treat as no-op.
    # We accept this regardless of output locale (Korean, Japanese, etc.).
    if rc == 1:
        return False
    raise GitCommandError(rc, stderr, ["git", "commit", "-m", message])


def push(path: str | Path, remote: str, branch: str) -> None:
    """Push *branch* to *remote*.

    Raises
    ------
    GitCommandError
        On non-zero exit from git push.
    """
    rc, _, stderr = _git("push", remote, branch, cwd=path)
    if rc != 0:
        raise GitCommandError(rc, stderr, ["git", "push", remote, branch])


def status(path: str | Path) -> dict[str, object]:
    """Return a dict describing the working-tree's sync state.

    Keys
    ----
    branch : str
        Current branch name.
    ahead : int
        Number of local commits not yet pushed to the tracked upstream.
    behind : int
        Number of upstream commits not yet pulled locally.
    dirty : bool
        ``True`` when there are uncommitted changes (staged or unstaged).
    remote : str | None
        Name of the configured remote for the current branch, or ``None``.
    upstream : str | None
        Full upstream ref (e.g. ``"origin/main"``), or ``None`` when no
        upstream tracking branch is configured.
    untracked : int
        Number of untracked files.
    """
    result: dict[str, object] = {
        "branch": "",
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "remote": None,
        "upstream": None,
        "untracked": 0,
    }

    # Current branch
    try:
        result["branch"] = current_branch(path)
    except GitCommandError:
        pass

    # Ahead / behind relative to upstream
    rc, stdout, _ = _git(
        "rev-list", "--count", "--left-right", "@{upstream}...HEAD", cwd=path
    )
    if rc == 0:
        parts = stdout.strip().split()
        if len(parts) == 2:
            result["behind"] = int(parts[0])
            result["ahead"] = int(parts[1])

    # Upstream tracking ref
    rc2, out2, _ = _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", cwd=path
    )
    if rc2 == 0:
        upstream_ref = out2.strip()
        result["upstream"] = upstream_ref
        # Derive remote name from the upstream ref (e.g. "origin/main" → "origin")
        if "/" in upstream_ref:
            result["remote"] = upstream_ref.split("/", 1)[0]

    # Dirty state (staged or unstaged changes)
    rc3, out3, _ = _git("status", "--porcelain", cwd=path)
    if rc3 == 0:
        lines = [l for l in out3.splitlines() if l.strip()]
        tracked = [l for l in lines if not l.startswith("??")]
        untracked = [l for l in lines if l.startswith("??")]
        result["dirty"] = len(tracked) > 0
        result["untracked"] = len(untracked)

    return result
