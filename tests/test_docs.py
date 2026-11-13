"""The submission docs keep their hard limits (brief §4, PRD step 12)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TECHNICAL_NOTE = ROOT / "docs" / "technical-note.md"
MAX_WORDS = 500


def _words(text: str) -> int:
    """Words as `wc -w` counts them (whitespace-separated), after removing HTML comments."""
    return len(re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL).split())


def test_technical_note_is_at_most_500_words():
    words = _words(TECHNICAL_NOTE.read_text(encoding="utf-8"))

    assert 0 < words <= MAX_WORDS, f"docs/technical-note.md has {words} words; the limit is {MAX_WORDS}"


def test_word_count_ignores_html_comments_only():
    assert _words("<!-- a draft\nnote -->\none two\nthree") == 3
