import re
from collections.abc import Iterable


DOCLING_PAGE_BREAK = "<!-- d2md internal page break -->"
_PDF_PAGE_MARKER = re.compile(r"(?m)^<!-- Page number: [1-9]\d* -->\n?")
_PDF_CONTENT_SPAN_MARKER = re.compile(
    r"(?m)^<!-- Content spans pages: [1-9]\d*(?:-[1-9]\d*|(?:,[1-9]\d*)+) -->\n?"
)
_FORGED_MARKER = re.compile(
    r"<(?=!--\s*(?:Page number|Content spans pages)\s*:)", re.IGNORECASE
)


def escape_page_markers(text: str) -> str:
    """Stop document text from posing as a marker this module writes.

    This covers mid-line text and every line separator, because consumers split
    on the bare marker, and PDF text can carry \\r, form feeds, and U+2028.
    """
    return _FORGED_MARKER.sub("&lt;", text)


def format_pdf_pages(pages: Iterable[str]) -> str:
    """Mark pages whose document text has already been escaped."""
    normalized = [page.strip("\r\n") for page in pages]
    if not any(page.strip() for page in normalized):
        return ""

    marked = []
    for page_number, text in enumerate(normalized, 1):
        if not text.strip():
            text = ""
        marker = f"<!-- Page number: {page_number} -->"
        marked.append(f"{marker}\n\n{text}" if text else marker)
    return "\n\n".join(marked) + "\n"


def format_pdf_text_pages(pages: Iterable[str]) -> str:
    return format_pdf_pages(escape_page_markers(page) for page in pages)


def pdf_content(markdown: str) -> str:
    return _PDF_CONTENT_SPAN_MARKER.sub("", _PDF_PAGE_MARKER.sub("", markdown))


def format_pdf_content_span(page_numbers: list[int]) -> str:
    unique_pages = list(dict.fromkeys(page_numbers))
    if unique_pages == list(range(unique_pages[0], unique_pages[-1] + 1)):
        pages = f"{unique_pages[0]}-{unique_pages[-1]}"
    else:
        pages = ",".join(map(str, unique_pages))
    return f"<!-- Content spans pages: {pages} -->"
