"""One structured-output LLM call, shared by sample.py and gate.py.

Default backend: the local `claude` CLI in print mode with a JSON Schema, no tools, no MCP servers,
no settings and no saved session, so a prompt (which can hold raw utterances) is sent once and
nothing about it is written to disk.

To use another model, set RSI_LLM_CMD to a command that reads one JSON object on stdin
({"system", "prompt", "schema", "model"}) and prints one JSON object matching the schema on stdout.
It must not log or store its input.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor


class LlmError(Exception):
    pass


def _once(system, prompt, schema, model, timeout):
    custom = os.environ.get("RSI_LLM_CMD")
    cwd = tempfile.gettempdir()
    if custom:
        body = json.dumps({"system": system, "prompt": prompt, "schema": schema, "model": model})
        p = subprocess.run(shlex.split(custom), input=body, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        out = json.loads(p.stdout)
        if not isinstance(out, dict):
            raise LlmError("no structured output")
        return out
    cmd = [os.environ.get("RSI_LLM_BIN", "claude"), "-p", "--model", model, "--tools", "",
           "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "",
           "--system-prompt", system, "--output-format", "json", "--json-schema", json.dumps(schema)]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    d = json.loads(p.stdout)
    if d.get("is_error") or not isinstance(d.get("structured_output"), dict):
        raise LlmError("no structured output")
    return d["structured_output"]


def call(system: str, prompt: str, schema: dict, model: str = "sonnet", timeout: int = 240, retries: int = 2) -> dict:
    last = None
    for attempt in range(retries + 1):
        try:
            return _once(system, prompt, schema, model, timeout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, LlmError, OSError) as e:
            last = e
            time.sleep(2 + 4 * attempt)
    raise LlmError(f"LLM call failed after {retries + 1} tries: {type(last).__name__}")   # never echo the prompt


def map_calls(fn, items, workers: int = 6):
    """Run fn over items in a small pool, keeping order. fn must catch its own errors."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))
