# apprentice

An MCP server for Claude Code that delegates scoped coding subtasks to cheap/fast models — while your main session stays on normal claude.ai subscription auth.

## Why

Claude Code has one `ANTHROPIC_BASE_URL` per session. Point it at a gateway (like [litellm](https://github.com/BerriAI/litellm)) to reach cheaper or local models, and per Anthropic's own docs, the whole session leaves subscription billing — there's no way to route *some* traffic through a gateway and the rest through your subscription within one process.

MCP tools sidestep this. A tool call is just a function the model invokes and reads a result from — it doesn't change which LLM is answering the conversation or how that's billed. So the main session stays on subscription auth, and "have a cheap model draft this" becomes a tool call the planner model chooses to make, same as any other tool.

The intended shape: a capable model (Opus, Fable, whatever you're paying for) acts as planner/architect/judge, and delegates the bulk of mechanical implementation work to cheap or free models, reviewing every result before it's kept.

## How it works

```
Claude Code (main session, subscription auth)
        │
        │ tool call: delegate_code_task(task, file_paths, model)
        ▼
   apprentice (this server)
        │
        │ 1. git worktree add --detach <tmp> HEAD
        │ 2. replicate uncommitted changes onto it (tracked + untracked)
        │ 3. commit that baseline — in the worktree only
        │ 4. call the worker model with the task + file contents
        │ 5. write the worker's output into the worktree
        │ 6. git diff HEAD --cached  →  this is what gets returned
        │ 7. copy changed files into your real working tree (atomic)
        │ 8. remove the worktree
        ▼
   your litellm proxy (or any OpenAI-compatible /v1/chat/completions endpoint)
```

Every draft happens in an isolated git worktree first — nothing touches your real working tree until the tool has a complete, valid result to apply. If the result looks wrong, `revert_files` undoes it (`git checkout HEAD`, or delete if the task created the file) with no further model calls needed.

The tool returns a diff, not full file content — Claude reviews a small diff instead of paying input tokens to receive the whole file and output tokens to re-emit it through its own `Edit` tool. The generation cost lives entirely on the cheap model's side.

## Requirements

- Python 3.11+ (verified on 3.13 and 3.14)
- A running OpenAI-compatible proxy exposing the worker models — [litellm](https://github.com/BerriAI/litellm) is what this was built against. Note: litellm's own proxy has a separate, unrelated Python 3.14 issue (see [Known issues](#known-issues)) — that's about running litellm, not this project.
- Every target directory must be a git repository — `apprentice` refuses to operate on anything else

## Install

```bash
git clone git@github.com:khanghoang/Apprentice.git ~/code/apprentice
cd ~/code/apprentice
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Point it at your proxy and give it a master key, either via env vars or a key file:

```bash
export APPRENTICE_PROXY_URL="http://localhost:4000"   # default shown
export APPRENTICE_MASTER_KEY="sk-..."                  # or:
```

If `APPRENTICE_MASTER_KEY` isn't set, it falls back to reading `~/code/litellm/.master_key.local`.

## Register with Claude Code

```bash
claude mcp add --scope user apprentice -- ~/code/apprentice/.venv/bin/apprentice-mcp
```

Verify:

```bash
claude mcp list
# apprentice: ... - ✔ Connected
```

## Tools

### `delegate_code_task(task, file_paths, context="", model="claude-cloud-fast")`

Delegates a scoped coding change to a worker model.

- **`task`** — precise description of the change. The more specific (signatures, expected behavior, edge cases), the better a small model does.
- **`file_paths`** — every file the task should read and/or write. Files that don't exist yet are created. Keep this to files that are actually part of the change — tasks meant to run in parallel shouldn't share files.
- **`context`** — optional extra context not already present in the listed files.
- **`model`** — which backing model to use, matching a `model_list` entry in your proxy config.

Returns a unified diff of what changed, already applied to your working tree.

### `revert_files(file_paths)`

Restores the given files to their last-committed state, or deletes them if a task created them from scratch. No model call involved.

## Design notes

- **Per-task worktrees, not a shared one.** Each `delegate_code_task` call creates and tears down its own worktree, so parallel tool calls (Claude can dispatch several at once) don't collide.
- **Baseline includes uncommitted work.** Each worktree is seeded from `HEAD` *plus* whatever's currently uncommitted in your main tree (tracked and untracked) — not just the last commit — so a delegated task sees the same reality you're looking at.
- **Multi-file is atomic.** The worker model returns a JSON object mapping every requested path to its complete new content; applying to the main tree reads every file before writing any of them, so a task either fully lands or fully doesn't.
- **`git diff HEAD` alone misses untracked files.** The diff step stages (`git add -A`) inside the worktree before diffing — otherwise a newly created file silently doesn't show up in the returned diff.

Full design writeup, including the token-cost reasoning behind "return a diff, not content": see the companion vault note if you have access to it, or `apprentice/server.py`'s module docstring.

## Known issues

- **litellm's proxy (not apprentice) breaks on Python 3.14** with `ImportError: cannot import name 'BaseDefaultEventLoopPolicy' from 'asyncio.events'` — a `uvloop`/uvicorn issue in litellm's dependency chain. If your litellm proxy hits this, run it under 3.13 instead; apprentice itself has no such constraint.
