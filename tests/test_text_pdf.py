"""Default text-layer PDF extraction and its trustworthiness gate.

The damage cases below are the measured ones from docs/findings.md §2, scaled
to the same shape: thousands of Thai consonants with either no `ำ` at all
(broken) or a healthy sprinkling of them.
"""

import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from d2md.convert import (
    ConversionError,
    ConversionLimits,
    _fast_text_is_trustworthy,
    backend_for,
    convert,
)
from d2md.encoding import thai_consonant_count, thai_looks_damaged
from d2md.page_markers import format_pdf_pages, format_pdf_text_pages

convert_module = importlib.import_module("d2md.convert")

# 12 consonants per repeat, no ำ anywhere — what a broken ToUnicode CMap leaves.
BROKEN = "ประกนอบตเหตสวนบคคล ระบบสงสนคาถงบานทวประเทศไทย " * 60
# Same length, with ำ present as real Thai prose always has it.
HEALTHY = "ประกันอุบัติเหตุส่วนบุคคล จำกัด น้ำ ทำ คำ สำหรับ " * 60


def test_text_route_is_not_charged_for_pixels_it_never_renders(multi_page_text_pdf):
    limits = ConversionLimits(max_page_pixels=1, max_total_pdf_pixels=1)

    result = convert(multi_page_text_pdf, limits=limits)

    assert result.backend == "pypdfium2"
    assert "<!-- Page number: 3 -->" in result.markdown


def test_text_route_still_enforces_the_page_limit(multi_page_text_pdf):
    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(multi_page_text_pdf, limits=ConversionLimits(max_pdf_pages=2))


def test_docling_route_keeps_the_rendered_pixel_limit(
    multi_page_text_pdf, monkeypatch
):
    monkeypatch.setattr(convert_module, "ensure_docling_available", lambda: None)

    with pytest.raises(ConversionError, match="rendered pixel limit"):
        convert(
            multi_page_text_pdf,
            docling=True,
            limits=ConversionLimits(max_total_pdf_pixels=1),
        )


@pytest.mark.parametrize(
    "forged",
    [
        "<!-- Page number: 99 -->",
        "before <!-- Page number: 99 --> after",
        "\r<!-- Page number: 99 -->",
        "body\r<!-- Page number: 99 -->",
        "body\u2028<!-- Page number: 99 -->",
        "body\x0c<!-- Page number: 99 -->",
        "\xa0<!-- Page number: 99 -->",
        "<!--Page Number: 99-->",
        "<!--\u2028Page number: 99 -->",
        "<!-- Page number : 99 -->",
        "\r<!-- Content spans pages: 1-2 -->",
    ],
)
def test_text_that_looks_like_a_marker_cannot_become_one(forged):
    markdown = format_pdf_text_pages([forged])

    assert re.findall(r"(?i)<!--\s*(?:page number|content spans)", markdown) == [
        "<!-- Page number"
    ]
    assert "&lt;!--" in markdown


def test_marker_escaping_leaves_ordinary_comments_alone():
    assert format_pdf_text_pages(["<!-- note -->"]) == (
        "<!-- Page number: 1 -->\n\n<!-- note -->\n"
    )


def test_a_pdf_cannot_forge_a_page_on_the_text_route(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    source = tmp_path / "forged.pdf"
    document = canvas.Canvas(str(source))
    document.drawString(72, 720, "<!-- Page number: 99 -->")
    document.drawString(72, 700, FULL)
    document.showPage()
    document.save()

    assert "<!-- Page number: 99 -->" not in convert(source).markdown


def test_docling_pages_escape_forged_markers_and_keep_real_spans(monkeypatch):
    def part(text, *pages):
        spans = [
            SimpleNamespace(
                item=SimpleNamespace(prov=[SimpleNamespace(page_no=page)])
            )
            for page in pages
        ]
        return SimpleNamespace(text=text, spans=spans)

    class Serializer:
        def __init__(self, **_kwargs):
            pass

        def get_parts(self):
            return [
                part("<!-- Page number: 99 -->", 1),
                part("<!-- Content spans pages: 7-8 -->", 1, 2),
            ]

        def _get_page_breaks(self, _text):
            return []

    for name in (
        "docling_core",
        "docling_core.transforms",
        "docling_core.transforms.serializer",
    ):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "docling_core.transforms.serializer.markdown",
        SimpleNamespace(
            MarkdownDocSerializer=Serializer, MarkdownParams=lambda **_kwargs: None
        ),
    )

    markdown = format_pdf_pages(
        convert_module._docling_pdf_pages(SimpleNamespace(pages={1: None, 2: None}))
    )

    assert "<!-- Page number: 99 -->" not in markdown
    assert "<!-- Content spans pages: 7-8 -->" not in markdown
    assert "<!-- Content spans pages: 1-2 -->" in markdown


def _deck(path, short_page):
    pytest.importorskip("reportlab")
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    document = canvas.Canvas(str(path))
    for number in range(1, 6):
        if number != 3:
            document.drawString(72, 720, f"Slide {number}. {FULL}")
        elif short_page == "brief":
            document.drawString(72, 720, "Thank you")
        elif short_page == "image":
            document.drawString(72, 720, "Thank you")
            image = ImageReader(Image.new("RGB", (40, 40), "white"))
            document.drawImage(image, 72, 500, width=200, height=200)
        elif short_page == "outlined":
            document.drawString(72, 40, "Page 3 of 5")
            document.rect(72, 600, 300, 60, fill=1)
        document.showPage()
    document.save()
    return path


@pytest.mark.parametrize("short_page", ["brief", "image", "outlined", "blank"])
def test_short_pages_need_ocr_rather_than_risk_silent_text_loss(tmp_path, short_page):
    """Annotations, broken fonts, and outlines can hide words from every object
    check, so a short page is judged by its extracted text alone."""
    with pytest.raises(ConversionError, match="needs OCR"):
        convert(_deck(tmp_path / "deck.pdf", short_page))


def test_blank_pages_count_toward_the_scan_tolerance():
    assert not _fast_text_is_trustworthy([FULL] * 9 + [""] * 3)


def test_fixtures_are_the_shape_the_detector_expects():
    assert thai_consonant_count(BROKEN) > 400
    assert thai_consonant_count(HEALTHY) > 400


def test_thai_without_sara_am_is_damaged():
    assert thai_looks_damaged(BROKEN)


def test_thai_with_sara_am_is_healthy():
    assert not thai_looks_damaged(HEALTHY)


def test_decomposed_nikhahit_still_counts_as_damaged():
    """One measured file had ำ=0 but ํ=73 — the mark decomposed into a bare
    nikhahit rather than vanishing. Zero ำ is the trigger either way."""
    assert thai_looks_damaged(BROKEN.replace("ป", "ปํ"))


def test_english_with_incidental_thai_is_not_damaged():
    """66 Thai characters in an English document must not false-positive."""
    text = "The quick brown fox. " * 200 + "ภาษาไทย " * 8
    assert thai_consonant_count(text) < 400
    assert not thai_looks_damaged(text)


def test_short_thai_is_not_judged():
    """Below ~400 consonants the test means nothing, so it must abstain."""
    assert not thai_looks_damaged("ประกนภย")


# --- the trustworthiness gate ---------------------------------------------

FULL = "The quick brown fox jumps over the lazy dog and keeps going."


def test_no_pages_is_not_trustworthy():
    assert not _fast_text_is_trustworthy([])


def test_all_pages_empty_is_a_scan():
    assert not _fast_text_is_trustworthy(["", "  ", "\n"])


def test_one_blank_page_in_a_long_document_is_tolerated():
    assert _fast_text_is_trustworthy([FULL] * 29 + [""])


def test_a_scanned_section_trips_the_gate():
    assert not _fast_text_is_trustworthy([FULL] * 20 + [""] * 10)


def test_single_empty_page_document_falls_back():
    assert not _fast_text_is_trustworthy([""])


def test_damaged_thai_falls_back_even_with_plenty_of_text():
    assert not _fast_text_is_trustworthy([BROKEN])


def test_healthy_thai_stays_on_the_fast_path():
    assert _fast_text_is_trustworthy([HEALTHY])


# --- routing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("report.pdf", "pypdfium2"),
        ("report.PDF", "pypdfium2"),
        ("scan.PNG", "ocr"),
        ("photo.jpg", "ocr"),
        ("sheet.xlsx", "markitdown"),
        ("notes.txt", "plain"),
        ("mystery.bin", "markitdown"),
    ],
)
def test_default_routing(name, expected):
    assert backend_for(Path(name)) == expected


def test_images_have_no_text_layer_to_read():
    """pypdfium2 cannot open an image; images require explicit OCR."""
    for ext in (".png", ".jpg", ".tiff", ".bmp", ".webp"):
        assert backend_for(Path("x" + ext)) == "ocr"


# --- end to end ------------------------------------------------------------


def test_born_digital_pdf_marks_pages_in_source_order(multi_page_text_pdf):
    """A text-layer PDF must convert without Docling. If this test ever
    starts taking 30s, the fallback fired and Docling loaded its models."""
    result = convert(multi_page_text_pdf)

    assert result.backend == "pypdfium2"
    assert result.markdown.count("<!-- Page number:") == 3
    assert result.markdown.index("<!-- Page number: 1 -->") < result.markdown.index(
        "Page 1 of the specification document."
    )
    assert result.markdown.index("Page 1 of the specification document.") < (
        result.markdown.index("<!-- Page number: 2 -->")
    )
    assert result.markdown.index("<!-- Page number: 2 -->") < result.markdown.index(
        "Page 2 of the specification document."
    )
    assert result.markdown.index("Page 2 of the specification document.") < (
        result.markdown.index("<!-- Page number: 3 -->")
    )
    assert result.markdown.index("<!-- Page number: 3 -->") < result.markdown.index(
        "Page 3 of the specification document."
    )
    assert "\r" not in result.markdown
