"""apprentice: delegate scoped coding subtasks to cheap/fast models via a
litellm proxy, with git-worktree-isolated drafting so nothing touches your
working tree until you've reviewed a diff.

Design notes live in the second-brain vault:
  wiki/Delegating coding tasks to cheap models (apprentice).md
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import worker, worktree

DEFAULT_PROXY_URL = os.environ.get("APPRENTICE_PROXY_URL", "http://localhost:4000")
DEFAULT_MASTER_KEY_FILE = Path.home() / "code" / "litellm" / ".master_key.local"

mcp = FastMCP("apprentice")


def _master_key() -> str:
    key = os.environ.get("APPRENTICE_MASTER_KEY")
    if key:
        return key
    if DEFAULT_MASTER_KEY_FILE.exists():
        return DEFAULT_MASTER_KEY_FILE.read_text().strip()
    raise RuntimeError(
        "No litellm master key found. Set APPRENTICE_MASTER_KEY or ensure "
        f"{DEFAULT_MASTER_KEY_FILE} exists."
    )


@mcp.tool()
def delegate_code_task(
    task: str,
    file_paths: list[str],
    context: str = "",
    model: str = "claude-cloud-fast",
) -> str:
    """Delegate a scoped coding change to a cheap/fast worker model.

    Drafts the change in an isolated git worktree (seeded with the current
    state of your repo, including uncommitted work) so nothing touches the
    real working tree until you review the returned diff and decide it's
    good. Use this for mechanical, well-specified subtasks — not open-ended
    or judgment-heavy work.

    Args:
        task: Precise description of the change to make. The more specific
            the spec (signatures, expected behavior, edge cases), the better
            a small model does.
        file_paths: Every file this task should read and/or write. Files
            that don't exist yet are created. Keep this list to files that
            are genuinely part of THIS change — a task touching unrelated
            files can't safely run in parallel with siblings that share them.
        context: Optional extra context not already present in the listed
            files (a type signature from elsewhere, a convention to follow).
        model: Which backing model to use — one of the model_list entries in
            the litellm proxy config (e.g. "claude-cloud-fast" for Groq,
            "claude-cloud-reasoning" for OpenRouter). Pick a heavier one for
            tasks needing more than mechanical work.

    Returns:
        A unified diff of exactly what this task changed, already applied to
        your working tree. Review it; call revert_files if it's wrong.
    """
    repo_root = worktree.find_git_root(Path.cwd())
    wt = worktree.create_worktree(repo_root)
    try:
        current_contents: dict[str, str | None] = {}
        for rel in file_paths:
            src = wt.path / rel
            current_contents[rel] = src.read_text() if src.exists() else None

        new_files = worker.generate_files(
            task=task,
            files=current_contents,
            context=context,
            model=model,
            proxy_url=DEFAULT_PROXY_URL,
            master_key=_master_key(),
        )

        for rel, content in new_files.items():
            dst = wt.path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content)

        diff = worktree.compute_diff(wt)
        if not diff.strip():
            return "Worker model made no changes (returned identical content)."

        worktree.apply_to_main(wt, file_paths)
        return diff
    finally:
        worktree.teardown(wt)


@mcp.tool()
def revert_files(file_paths: list[str]) -> str:
    """Undo an applied delegate_code_task change on the given files, restoring
    them to their last-committed (HEAD) state, or deleting them if the task
    created them from scratch.
    """
    repo_root = worktree.find_git_root(Path.cwd())
    worktree.revert_files(repo_root, file_paths)
    return f"Reverted: {', '.join(file_paths)}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
