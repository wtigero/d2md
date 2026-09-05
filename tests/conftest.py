from pathlib import Path

import pytest


@pytest.fixture
def multi_page_text_pdf(tmp_path: Path) -> Path:
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    source = tmp_path / "multi-page.pdf"
    document = canvas.Canvas(str(source))
    for page_number in range(1, 4):
        document.drawString(
            72,
            720,
            f"Page {page_number} of the specification document.",
        )
        document.drawString(
            72,
            700,
            "The quick brown fox jumps over the lazy dog and keeps going.",
        )
        document.showPage()
    document.save()
    return source
