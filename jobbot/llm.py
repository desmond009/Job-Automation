"""Calls Claude through the Claude Code CLI in headless mode, so it runs on your
existing Claude login and needs no separate API key."""
import json
import re
import subprocess
import tempfile


class ClaudeError(RuntimeError):
    pass


def ask(prompt, cfg, timeout=300):
    cmd = [
        cfg["claude"]["bin"], "-p", "--model", cfg["claude"]["model"], "--output-format", "json",
        "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory() as cwd:  # keep any project CLAUDE.md out of the prompt
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if r.returncode != 0:
        raise ClaudeError((r.stderr or r.stdout).strip()[:500])
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise ClaudeError(str(out.get("result"))[:500])
    return out["result"]


def ask_json(prompt, cfg):
    """Ask for JSON and parse the first object or array in the reply."""
    text = ask(prompt, cfg)
    # raw_decode stops at the end of the first value, so prose after the JSON is ignored.
    match = re.search(r"[\[{]", text)
    if not match:
        raise ClaudeError(f"no JSON in reply: {text[:300]}")
    return json.JSONDecoder().raw_decode(text, match.start())[0]
