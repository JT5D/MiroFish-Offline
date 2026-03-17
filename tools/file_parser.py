"""
Reusable File Parser with Encoding Detection & Text Chunking
Extracted from MiroFish-Offline.

Supports PDF (via PyMuPDF), Markdown, and TXT files with
multi-level encoding fallback for non-UTF-8 files.

Usage:
    from tools.file_parser import FileParser, split_text_into_chunks

    text = FileParser.extract_text("paper.pdf")
    chunks = split_text_into_chunks(text, chunk_size=500, overlap=50)

    # Batch extraction:
    combined = FileParser.extract_from_multiple(["a.pdf", "b.md", "c.txt"])
"""

import os
from pathlib import Path
from typing import List


def _read_text_with_fallback(file_path: str) -> str:
    """
    Read a text file with multi-level encoding fallback:
      1. UTF-8
      2. charset_normalizer detection
      3. chardet detection
      4. UTF-8 with errors='replace'
    """
    data = Path(file_path).read_bytes()

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    encoding = None
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best and best.encoding:
            encoding = best.encoding
    except Exception:
        pass

    if not encoding:
        try:
            import chardet
            result = chardet.detect(data)
            encoding = result.get("encoding") if result else None
        except Exception:
            pass

    return data.decode(encoding or "utf-8", errors="replace")


class FileParser:
    """Extract text from PDF, Markdown, and TXT files."""

    SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}

    @classmethod
    def extract_text(cls, file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = path.suffix.lower()
        if suffix not in cls.SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported format: {suffix}")

        if suffix == ".pdf":
            return cls._extract_from_pdf(file_path)
        return _read_text_with_fallback(file_path)

    @staticmethod
    def _extract_from_pdf(file_path: str) -> str:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ImportError("PyMuPDF required: pip install PyMuPDF")

        parts = []
        with fitz.open(file_path) as doc:
            for page in doc:
                text = page.get_text()
                if text.strip():
                    parts.append(text)
        return "\n\n".join(parts)

    @classmethod
    def extract_from_multiple(cls, file_paths: List[str]) -> str:
        texts = []
        for i, fp in enumerate(file_paths, 1):
            try:
                text = cls.extract_text(fp)
                texts.append(f"=== Document {i}: {Path(fp).name} ===\n{text}")
            except Exception as e:
                texts.append(f"=== Document {i}: {fp} (failed: {e}) ===")
        return "\n\n".join(texts)


def split_text_into_chunks(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[str]:
    """
    Split text into overlapping chunks, preferring sentence boundaries.
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end < len(text):
            for sep in [".\n", "!\n", "?\n", "\n\n", ". ", "! ", "? "]:
                last_sep = text[start:end].rfind(sep)
                if last_sep != -1 and last_sep > chunk_size * 0.3:
                    end = start + last_sep + len(sep)
                    break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap if end < len(text) else len(text)

    return chunks
