"""Calls the litellm proxy to get a multi-file code draft as structured JSON."""

from __future__ import annotations

import json
import re

import requests

_SYSTEM = """You are implementing a small, precisely scoped code change on behalf of a senior engineer who will review your work before it's applied. You will not receive credit for extra changes outside what was asked — stay exactly within the given files and task.

Output ONLY a single JSON object, nothing else — no markdown code fences, no explanation before or after. Shape:

{"files": {"<path>": "<complete new file content>", ...}}

Rules:
- Every path listed under FILES below must appear as a key in your output.
- Each value is the file's COMPLETE new content (not a diff, not a snippet) — exactly what the file should contain after your change.
- If a file is marked (new file), you are creating it from scratch.
- Do not touch files that were not listed."""


class WorkerError(RuntimeError):
    pass


def _build_prompt(task: str, files: dict[str, str | None], context: str) -> str:
    parts = [f"TASK:\n{task}"]
    if context.strip():
        parts.append(f"ADDITIONAL CONTEXT:\n{context}")
    parts.append("FILES:")
    for path, content in files.items():
        if content is None:
            parts.append(f"--- {path} (new file) ---\n(empty)")
        else:
            parts.append(f"--- {path} ---\n{content}")
    return "\n\n".join(parts)


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    # Strip markdown fences if the model added them despite instructions.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the first {...} block in case of stray prose around it.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON object found in model output")


def _call_once(prompt: str, model: str, proxy_url: str, master_key: str) -> str:
    resp = requests.post(
        f"{proxy_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {master_key}", "content-type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise WorkerError(f"worker model call failed ({resp.status_code}): {resp.text[:500]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise WorkerError(f"unexpected response shape from proxy: {data}") from exc


def generate_files(
    task: str,
    files: dict[str, str | None],
    context: str,
    model: str,
    proxy_url: str,
    master_key: str,
) -> dict[str, str]:
    """Returns {path: new_content}. Raises WorkerError if the model can't be
    coaxed into valid JSON after one retry, or if it omits a requested file."""
    prompt = _build_prompt(task, files, context)
    raw = _call_once(prompt, model, proxy_url, master_key)

    try:
        parsed = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        # One retry, telling the model exactly what went wrong.
        retry_prompt = (
            prompt
            + f"\n\nYour previous response could not be parsed as JSON ({exc}). "
            "Reply again with ONLY the JSON object described in the system prompt — "
            "no fences, no commentary."
        )
        raw = _call_once(retry_prompt, model, proxy_url, master_key)
        try:
            parsed = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc2:
            raise WorkerError(
                f"model did not return valid JSON after retry: {exc2}\nraw output: {raw[:1000]}"
            ) from exc2

    result = parsed.get("files")
    if not isinstance(result, dict):
        # Some models skip the {"files": {...}} wrapper and return the path
        # -> content mapping directly at the top level despite instructions.
        # Accept that shape too, but only when every top-level key is one of
        # the files actually requested — otherwise this would silently treat
        # some other malformed structure as a valid files mapping.
        if parsed and all(k in files for k in parsed):
            result = parsed
        else:
            raise WorkerError(f"model output missing top-level 'files' object: {parsed}")

    missing = [p for p in files if p not in result]
    if missing:
        raise WorkerError(f"model omitted requested file(s): {missing}")

    return result
