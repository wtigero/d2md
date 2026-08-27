import importlib.util
from typing import Literal
from .errors import ConversionError
from .ocr import available_engines


def install_command(extra: Literal["ocr", "docling"], *, force: bool) -> str:
    option = " --force" if force else ""
    return f'uv tool install{option} "d2md[{extra}]"'


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def ensure_ocr_available() -> None:
    if not available_engines():
        raise ConversionError(
            f"OCR is not installed. Run: {install_command('ocr', force=True)}, "
            "then rerun with --ocr"
        )


def ensure_docling_available() -> None:
    if not module_available("docling"):
        raise ConversionError(
            f"Docling is not installed. Run: {install_command('docling', force=True)}, "
            "then rerun with --docling"
        )
