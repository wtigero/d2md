"""Bounded direct OCR for images and scanned PDFs."""

from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

from .errors import ConversionError
from .ocr import engine_for, read
from .page_markers import format_pdf_text_pages

if TYPE_CHECKING:
    from PIL.Image import Image


OCR_RENDER_SCALE = 2.0
# Without this, Pillow sniffs any format it knows. An EPS named .png runs gs.
IMAGE_FORMATS = ("PNG", "JPEG", "TIFF", "BMP", "WEBP")
ENGINE_BACKENDS = {"vision": "ocrmac", "rapidocr": "rapidocr"}


class OcrLimits(Protocol):
    max_pdf_pages: int | None
    max_page_pixels: int | None
    max_total_pdf_pixels: int | None
    max_output_chars: int | None


def page_pixels(page, scale: float = OCR_RENDER_SCALE) -> int:
    width, height = page.get_size()
    return ceil(width * scale) * ceil(height * scale)


def check_limit(
    value: int | float, limit: int | None, label: str, name: str | Path
) -> None:
    if limit is not None and value > limit:
        display = name.name if isinstance(name, Path) else name
        raise ConversionError(
            f"{label} limit exceeded: {display} has {value:,}; maximum is {limit:,}"
        )


def image_pages(image, limits: OcrLimits, path: Path):
    """Yield each scanned page within limits; only TIFF is treated as paged."""
    if image.format == "TIFF":
        from PIL import ImageSequence

        pages = ImageSequence.Iterator(image)
    else:
        pages = (image,)

    total_pixels = 0
    for number, page in enumerate(pages, 1):
        check_limit(number, limits.max_pdf_pages, "image page", path)
        pixels = page.width * page.height
        check_limit(pixels, limits.max_page_pixels, "image pixel", path)
        total_pixels += pixels
        check_limit(total_pixels, limits.max_total_pdf_pixels, "total image pixel", path)
        yield page


def convert_with_ocr(
    path: Path, script: str, limits: OcrLimits
) -> tuple[str, str]:
    engine = engine_for(script)
    output = convert_scanned(
        path, limits, lambda image: read(image, script, engines=[engine]).text
    )
    return output, ENGINE_BACKENDS[engine]


def convert_scanned(
    path: Path, limits: OcrLimits, read_page: Callable[["Image"], str]
) -> str:
    """Render and read one page at a time, preserving source page order."""
    texts: list[str] = []
    total_chars = 0
    is_pdf = path.suffix.lower() == ".pdf"

    def append_page(page) -> None:
        nonlocal total_chars
        with page.convert("RGB") as image:
            text = read_page(image)
        texts.append(text)
        total_chars += len(text)
        check_limit(total_chars, limits.max_output_chars, "output", path)

    if is_pdf:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(path))
        try:
            check_limit(len(document), limits.max_pdf_pages, "PDF page", path)
            total_pixels = 0
            for index in range(len(document)):
                page = document[index]
                try:
                    pixels = page_pixels(page)
                    check_limit(
                        pixels,
                        limits.max_page_pixels,
                        "rendered page pixel",
                        path,
                    )
                    total_pixels += pixels
                    check_limit(
                        total_pixels,
                        limits.max_total_pdf_pixels,
                        "total PDF rendered pixel",
                        path,
                    )
                    bitmap = page.render(scale=OCR_RENDER_SCALE)
                    try:
                        rendered = bitmap.to_pil()
                        try:
                            append_page(rendered)
                        finally:
                            rendered.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        finally:
            document.close()
    else:
        from PIL import Image

        with Image.open(path, formats=IMAGE_FORMATS) as source:
            for page in image_pages(source, limits, path):
                append_page(page)

    if is_pdf:
        output = format_pdf_text_pages(texts)
    else:
        markdown = "\n\n".join(text.strip() for text in texts if text.strip()).strip()
        output = markdown + "\n" if markdown else ""
    check_limit(len(output), limits.max_output_chars, "output", path)
    return output
