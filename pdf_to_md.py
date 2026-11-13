"""Convert a PDF to Markdown using Docling.

Usage:
    python pdf_to_md.py <input.pdf> [output.md]
"""

import sys
from pathlib import Path

from docling.document_converter import DocumentConverter


def pdf_to_markdown(pdf_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Read a PDF with Docling and write it out as a Markdown file.

    Returns the path of the written .md file.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    output_path = Path(output_path) if output_path else pdf_path.with_suffix(".md")

    result = DocumentConverter().convert(pdf_path)
    markdown = result.document.export_to_markdown()

    output_path.write_text(markdown, encoding="utf-8")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python pdf_to_md.py <input.pdf> [output.md]")
    out = pdf_to_markdown(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"Wrote {out}")
