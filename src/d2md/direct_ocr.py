"""Bounded direct OCR for images and scanned PDFs."""

from math import ceil
from pathlib import Path
from typing import Protocol

from .errors import ConversionError
from .ocr import engine_for, read
from .page_markers import format_pdf_pages


OCR_RENDER_SCALE = 2.0
ENGINE_BACKENDS = {"vision": "ocrmac", "rapidocr": "rapidocr"}


class OcrLimits(Protocol):
    max_pdf_pages: int | None
    max_page_pixels: int | None
    max_total_pdf_pixels: int | None
    max_output_chars: int | None


def page_pixels(page, scale: float = OCR_RENDER_SCALE) -> int:
    width, height = page.get_size()
    return ceil(width * scale) * ceil(height * scale)


def _check(value: int, limit: int | None, label: str, path: Path) -> None:
    if limit is not None and value > limit:
        raise ConversionError(
            f"{label} limit exceeded: {path.name} has {value:,}; "
            f"maximum is {limit:,}"
        )


def convert_with_ocr(
    path: Path, script: str, limits: OcrLimits
) -> tuple[str, str]:
    engine = engine_for(script)
    texts: list[str] = []
    total_chars = 0

    if path.suffix.lower() == ".pdf":
        import pypdfium2

        document = pypdfium2.PdfDocument(str(path))
        try:
            _check(len(document), limits.max_pdf_pages, "PDF page", path)
            total_pixels = 0
            for index in range(len(document)):
                page = document[index]
                try:
                    pixels = page_pixels(page)
                    _check(
                        pixels,
                        limits.max_page_pixels,
                        "rendered page pixel",
                        path,
                    )
                    total_pixels += pixels
                    _check(
                        total_pixels,
                        limits.max_total_pdf_pixels,
                        "total PDF rendered pixel",
                        path,
                    )
                    bitmap = page.render(scale=OCR_RENDER_SCALE)
                    try:
                        rendered = bitmap.to_pil()
                        try:
                            image = rendered.convert("RGB")
                            try:
                                text = read(
                                    image, script, engines=[engine]
                                ).text
                            finally:
                                image.close()
                        finally:
                            rendered.close()
                    finally:
                        bitmap.close()
                    texts.append(text)
                    total_chars += len(text)
                    _check(
                        total_chars,
                        limits.max_output_chars,
                        "output",
                        path,
                    )
                finally:
                    page.close()
        finally:
            document.close()
    else:
        from PIL import Image

        with Image.open(path) as source:
            _check(
                source.width * source.height,
                limits.max_page_pixels,
                "image pixel",
                path,
            )
            image = source.convert("RGB")
            try:
                text = read(image, script, engines=[engine]).text
            finally:
                image.close()
        texts.append(text)
        _check(len(text), limits.max_output_chars, "output", path)

    if path.suffix.lower() == ".pdf":
        output = format_pdf_pages(texts)
    else:
        markdown = "\n\n".join(text.strip() for text in texts if text.strip()).strip()
        output = markdown + "\n" if markdown else ""
    _check(len(output), limits.max_output_chars, "output", path)
    return output, ENGINE_BACKENDS[engine]
