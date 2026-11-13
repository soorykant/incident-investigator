#!/usr/bin/env python3
"""Stop hook: export the current Claude Code session transcript into docs/transcript/.

Writes <session_id>.jsonl (raw copy) and <session_id>.md (readable) on every turn,
so the full history survives context compaction.
"""

import json
import os
import shutil
import sys
from pathlib import Path

MAX_TOOL_CHARS = 3000


def clip(text: str) -> str:
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + f"\n… [truncated {len(text) - MAX_TOOL_CHARS} chars]"


def render_block(block) -> str:
    if isinstance(block, str):
        return block
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        args = json.dumps(block.get("input", {}), indent=2, ensure_ascii=False)
        return f"**Tool call — `{block.get('name')}`**\n```json\n{clip(args)}\n```"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        return f"**Tool result**\n```\n{clip(str(content or ''))}\n```"
    return ""  # skip thinking, images, etc.


def to_markdown(jsonl_path: Path, session_id: str) -> str:
    out = [f"# Claude Code session transcript\n\nSession: `{session_id}`\n"]
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
            continue
        msg = entry.get("message") or {}
        content = msg.get("content")
        blocks = [content] if isinstance(content, str) else (content or [])
        body = "\n\n".join(b for b in (render_block(x) for x in blocks) if b.strip())
        if not body:
            continue
        role = "User" if entry["type"] == "user" else "Assistant"
        ts = entry.get("timestamp", "")
        out.append(f"---\n\n### {role} · {ts}\n\n{body}\n")
    return "\n".join(out)


def main() -> None:
    payload = json.load(sys.stdin)
    transcript = Path(payload["transcript_path"]).expanduser()
    session_id = payload.get("session_id") or transcript.stem
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".")
    dest = project / "docs" / "transcript"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(transcript, dest / f"{session_id}.jsonl")
    (dest / f"{session_id}.md").write_text(to_markdown(transcript, session_id), encoding="utf-8")


if __name__ == "__main__":
    main()
