"""Load an unstructured resume as UTF-8 text."""

from pathlib import Path


def load_resume(path: Path) -> str:
    """Return Markdown or plain text without imposing a field schema."""
    path = Path(path)
    if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ValueError("Resume must be a .md, .markdown, or .txt file")
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError(f"Resume is empty: {path}")
    return text
