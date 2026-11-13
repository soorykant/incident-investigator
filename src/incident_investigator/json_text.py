"""Find the JSON object in a model reply.

Models are asked to reply with exactly one JSON object, but they sometimes wrap it in a
code fence or add a sentence before or after it. `extract_json` returns the first
complete JSON object in the reply, so a well-formed answer is not thrown away for its
wrapping. When no complete object is found, it returns the stripped reply unchanged,
and the caller's parser reports the error as before.
"""

from __future__ import annotations

import json
import re

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)
_DECODER = json.JSONDecoder()


def extract_json(text: str) -> str:
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    start = stripped.find("{")
    while start != -1:
        try:
            _, end = _DECODER.raw_decode(stripped, start)
        except json.JSONDecodeError:
            start = stripped.find("{", start + 1)
            continue
        return stripped[start:end]
    return stripped


def reply_start(text: str, limit: int = 200) -> str:
    """The first `limit` characters of a reply, for a failure reason."""
    head = text.strip()
    return head if len(head) <= limit else head[:limit] + "..."
