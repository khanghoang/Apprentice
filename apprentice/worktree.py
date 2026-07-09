"""Git worktree isolation for delegated code drafts.

Each delegate_code_task call gets its own throwaway worktree, seeded with
the main tree's *current* state (HEAD + any uncommitted tracked/untracked
changes) so parallel tasks all draft against the same consistent baseline —
including work you haven't committed yet. The main tree is never touched
until a draft is explicitly applied.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git subprocess failed; message is the captured stderr."""


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}) failed:\n{result.stderr.strip()}")
    return result.stdout


def find_git_root(start: Path) -> Path:
    """Walk up from `start` to the repo root. Raises GitError if not a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(
            f"{start} is not inside a git repository — apprentice requires git "
            "so drafts can be safely isolated and reverted."
        )
    return Path(result.stdout.strip())


@dataclass
class Worktree:
    repo_root: Path
    path: Path


def create_worktree(repo_root: Path) -> Worktree:
    """Create an isolated worktree seeded with the main tree's current state
    (HEAD + uncommitted tracked changes + untracked files), then commit that
    baseline locally so the eventual diff isolates only what the worker model
    changes — not pre-existing uncommitted work.
    """
    wt_path = Path(tempfile.mkdtemp(prefix="apprentice-wt-"))
    # tempfile already created the dir; `git worktree add` wants to create it itself.
    wt_path.rmdir()

    _run(["worktree", "add", "--detach", str(wt_path), "HEAD"], cwd=repo_root)

    # Replicate uncommitted tracked changes (staged + unstaged) onto the worktree.
    uncommitted_diff = _run(["diff", "HEAD"], cwd=repo_root)
    if uncommitted_diff.strip():
        subprocess.run(
            ["git", "apply"],
            cwd=wt_path,
            input=uncommitted_diff,
            capture_output=True,
            text=True,
            check=False,
        )
        # Best-effort: if it fails to apply (rare — divergent state), the
        # worktree still has HEAD's content, which is a safe fallback.

    # Replicate untracked files (new files not yet added to git).
    status = _run(["status", "--porcelain"], cwd=repo_root)
    for line in status.splitlines():
        if line.startswith("??"):
            rel = line[3:].strip()
            src = repo_root / rel
            dst = wt_path / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    # Commit this baseline inside the worktree only (never touches the main
    # tree's history) so `git diff` after generation shows just the task's delta.
    _run(["add", "-A"], cwd=wt_path)
    # `commit` fails if there's nothing to commit (clean tree, no uncommitted
    # work) — that's fine, just means baseline == HEAD already.
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "apprentice: baseline snapshot", "--allow-empty"],
        cwd=wt_path,
        capture_output=True,
        text=True,
        check=False,
    )

    return Worktree(repo_root=repo_root, path=wt_path)


def compute_diff(wt: Worktree) -> str:
    """Diff of everything the worker model changed since the baseline commit.

    `git diff HEAD` alone never shows untracked (never-`git add`ed) files —
    a new file the worker model created would silently be missing from the
    diff. Stage everything first so new files are included.
    """
    _run(["add", "-A"], cwd=wt.path)
    return _run(["diff", "HEAD", "--cached"], cwd=wt.path)


def apply_to_main(wt: Worktree, file_paths: list[str]) -> None:
    """Copy the listed files from the worktree into the main tree, atomically
    (all-or-nothing: reads every source file before writing any of them)."""
    contents: dict[str, bytes | None] = {}
    for rel in file_paths:
        src = wt.path / rel
        contents[rel] = src.read_bytes() if src.exists() else None

    for rel, data in contents.items():
        dst = wt.repo_root / rel
        if data is None:
            # Worker model deleted the file (unusual, but handle it).
            dst.unlink(missing_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)


def revert_files(repo_root: Path, file_paths: list[str]) -> None:
    """Undo an applied change on the main tree: restore tracked files from
    HEAD, delete files that didn't exist in HEAD (newly created by a task)."""
    for rel in file_paths:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel}"],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        existed_at_head = result.returncode == 0
        if existed_at_head:
            _run(["checkout", "HEAD", "--", rel], cwd=repo_root)
        else:
            (repo_root / rel).unlink(missing_ok=True)


def teardown(wt: Worktree) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt.path)],
        cwd=wt.repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(["git", "worktree", "prune"], cwd=wt.repo_root, capture_output=True, check=False)
