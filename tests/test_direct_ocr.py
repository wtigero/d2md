from types import SimpleNamespace
import sys

import pytest
from PIL import Image

from d2md.direct_ocr import convert_with_ocr
from d2md.errors import ConversionError
from d2md.ocr import Reading


LIMITS = SimpleNamespace(
    max_pdf_pages=10,
    max_page_pixels=1_000_000,
    max_total_pdf_pixels=2_000_000,
    max_output_chars=1_000,
)


@pytest.fixture
def fake_pdfium(monkeypatch):
    events = []

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (40, 20), "white")

        def close(self):
            events.append("bitmap")

    class Page:
        def get_size(self):
            return 40, 20

        def render(self, scale):
            return Bitmap()

        def close(self):
            events.append("page")

    class Document:
        def __init__(self, path):
            self.pages = [Page(), Page()]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            events.append("document")

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        SimpleNamespace(PdfDocument=Document),
    )
    return events


def test_image_returns_text_and_actual_engine(tmp_path, monkeypatch):
    source = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(source)
    calls = []
    monkeypatch.setattr("d2md.direct_ocr.engine_for", lambda script: "vision")
    monkeypatch.setattr(
        "d2md.direct_ocr.read",
        lambda image, script, engines: calls.append(
            (image.mode, script, engines)
        )
        or Reading(script, "scanned text long enough", 1.0),
    )

    assert convert_with_ocr(source, "latin", LIMITS) == (
        "scanned text long enough\n",
        "ocrmac",
    )
    assert calls == [("RGB", "latin", ["vision"])]


def test_pdf_preserves_page_order(fake_pdfium, tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    texts = iter(("first page text", "second page text"))
    monkeypatch.setattr("d2md.direct_ocr.engine_for", lambda script: "rapidocr")
    monkeypatch.setattr(
        "d2md.direct_ocr.read",
        lambda image, script, engines: Reading(script, next(texts), 1.0),
    )

    assert convert_with_ocr(source, "latin", LIMITS) == (
        "first page text\n\nsecond page text\n",
        "rapidocr",
    )
    assert fake_pdfium.count("page") == 2
    assert fake_pdfium.count("bitmap") == 2
    assert fake_pdfium.count("document") == 1


def test_pdf_enforces_page_limit(fake_pdfium, tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    limits = SimpleNamespace(**{**LIMITS.__dict__, "max_pdf_pages": 1})
    monkeypatch.setattr(
        "d2md.direct_ocr.engine_for", lambda script: "rapidocr"
    )

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert_with_ocr(source, "latin", limits)


def test_pdf_enforces_page_pixels(fake_pdfium, tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    limits = SimpleNamespace(**{**LIMITS.__dict__, "max_page_pixels": 100})
    monkeypatch.setattr(
        "d2md.direct_ocr.engine_for", lambda script: "rapidocr"
    )

    with pytest.raises(ConversionError, match="rendered page pixel limit"):
        convert_with_ocr(source, "latin", limits)


def test_pdf_enforces_total_pixels(fake_pdfium, tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    limits = SimpleNamespace(**{**LIMITS.__dict__, "max_total_pdf_pixels": 1})
    monkeypatch.setattr(
        "d2md.direct_ocr.engine_for", lambda script: "rapidocr"
    )

    with pytest.raises(ConversionError, match="total PDF rendered pixel limit"):
        convert_with_ocr(source, "latin", limits)


def test_pdf_output_limit_counts_markdown_separators(
    fake_pdfium, tmp_path, monkeypatch
):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    limits = SimpleNamespace(**{**LIMITS.__dict__, "max_output_chars": 20})
    monkeypatch.setattr(
        "d2md.direct_ocr.engine_for", lambda script: "rapidocr"
    )
    monkeypatch.setattr(
        "d2md.direct_ocr.read",
        lambda image, script, engines: Reading(script, "1234567890", 1.0),
    )

    with pytest.raises(ConversionError, match="output limit"):
        convert_with_ocr(source, "latin", limits)


def test_image_enforces_pixel_limit(tmp_path, monkeypatch):
    source = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(source)
    limits = SimpleNamespace(**{**LIMITS.__dict__, "max_page_pixels": 100})
    monkeypatch.setattr("d2md.direct_ocr.engine_for", lambda script: "vision")

    with pytest.raises(ConversionError, match="image pixel limit"):
        convert_with_ocr(source, "latin", limits)


def test_empty_ocr_output_stays_empty_for_convert_to_reject(tmp_path, monkeypatch):
    source = tmp_path / "scan.png"
    Image.new("RGB", (40, 20), "white").save(source)
    monkeypatch.setattr("d2md.direct_ocr.engine_for", lambda script: "vision")
    monkeypatch.setattr(
        "d2md.direct_ocr.read",
        lambda image, script, engines: Reading(script, "  ", 1.0),
    )

    assert convert_with_ocr(source, "latin", LIMITS) == ("", "ocrmac")


def test_pdf_closes_resources_when_reader_fails(
    fake_pdfium, tmp_path, monkeypatch
):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr("d2md.direct_ocr.engine_for", lambda script: "rapidocr")
    monkeypatch.setattr(
        "d2md.direct_ocr.read",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("reader failed")
        ),
    )

    with pytest.raises(RuntimeError, match="reader failed"):
        convert_with_ocr(source, "latin", LIMITS)
    assert "page" in fake_pdfium
    assert "bitmap" in fake_pdfium
    assert "document" in fake_pdfium
