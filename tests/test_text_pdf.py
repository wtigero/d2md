"""Default text-layer PDF extraction and its trustworthiness gate.

The damage cases below are the measured ones from docs/findings.md §2, scaled
to the same shape: thousands of Thai consonants with either no `ำ` at all
(broken) or a healthy sprinkling of them.
"""

from pathlib import Path

import pytest

from d2md.convert import _fast_text_is_trustworthy, backend_for, convert
from d2md.encoding import thai_consonant_count, thai_looks_damaged

# 12 consonants per repeat, no ำ anywhere — what a broken ToUnicode CMap leaves.
BROKEN = "ประกนอบตเหตสวนบคคล ระบบสงสนคาถงบานทวประเทศไทย " * 60
# Same length, with ำ present as real Thai prose always has it.
HEALTHY = "ประกันอุบัติเหตุส่วนบุคคล จำกัด น้ำ ทำ คำ สำหรับ " * 60


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


def test_explicit_fast_values_are_deprecated_noops(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("plain text long enough for conversion")

    for value in (True, False):
        with pytest.warns(DeprecationWarning):
            assert convert(source, fast=value).backend == "plain"


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
