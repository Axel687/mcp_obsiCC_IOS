"""Extract text from PDFs stored inside the vault."""

from __future__ import annotations

from dataclasses import dataclass

from pypdf import PdfReader

from .vault import VaultError, resolve_inside_vault


@dataclass
class PdfExtract:
    path: str
    page_count: int
    pages: list[str]

    def joined(self, separator: str = "\n\n--- page {n} ---\n\n") -> str:
        parts: list[str] = []
        for i, text in enumerate(self.pages, start=1):
            parts.append(separator.format(n=i))
            parts.append(text)
        return "".join(parts).lstrip()


def read_pdf(path: str, page_start: int | None = None, page_end: int | None = None) -> PdfExtract:
    """Read a PDF from the vault. Pages are 1-indexed and inclusive."""
    target = resolve_inside_vault(path)
    if not target.is_file():
        raise VaultError(f"PDF not found: {path}")
    if target.suffix.lower() != ".pdf":
        raise VaultError(f"Not a .pdf file: {path}")

    reader = PdfReader(str(target))
    total = len(reader.pages)
    start = 1 if page_start is None else max(1, page_start)
    end = total if page_end is None else min(total, page_end)
    if start > end:
        raise VaultError(f"Empty page range: {start}..{end} (document has {total} pages)")

    pages: list[str] = []
    for idx in range(start - 1, end):
        try:
            pages.append(reader.pages[idx].extract_text() or "")
        except Exception as exc:  # pypdf can raise on malformed pages
            pages.append(f"[error extracting page {idx + 1}: {exc}]")
    return PdfExtract(path=path, page_count=total, pages=pages)
