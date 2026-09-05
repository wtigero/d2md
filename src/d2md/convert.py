"""Route files through only the explicitly requested conversion capabilities.

Backends can fail silently, so every conversion is checked for usable output
before it counts as a success.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import stat
import struct
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from ._onnx import disable_onnx_telemetry
from .capabilities import (
    ensure_docling_available,
    ensure_ocr_available,
    install_command,
)
from .direct_ocr import OCR_RENDER_SCALE, convert_with_ocr
from .encoding import read_text, thai_looks_damaged
from .errors import ConversionError
from .page_markers import (
    DOCLING_PAGE_BREAK,
    format_pdf_content_span,
    format_pdf_pages,
    pdf_content,
)

# markitdown wins here: it keeps sheet names, slide numbers and image alt text,
# and it is roughly an order of magnitude faster than a layout model.
OFFICE = {".docx", ".xlsx", ".xls", ".pptx", ".html", ".htm", ".msg", ".epub"}

IMAGES = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
PDFISH = {".pdf"} | IMAGES

PLAIN = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml"}

SUPPORTED = OFFICE | PDFISH | PLAIN

#: Below this many non-whitespace characters we treat the conversion as failed.
MIN_CHARS = 20

#: A page yielding fewer non-whitespace characters than this has no usable text
#: layer — almost always a scanned image.
EMPTY_PAGE_CHARS = 20

#: Fraction of empty pages above which the fast path hands the whole file to
#: Docling. Loose enough to tolerate the odd genuinely blank page in a long
#: document, tight enough that any real scanned section trips it.
SCAN_PAGE_FRACTION = 0.10

DEVICE_CHOICES = ("auto", "cpu", "cuda", "mps", "xpu")


def _normalize_device(device: str) -> str:
    if device not in DEVICE_CHOICES:
        raise ConversionError(
            f"unknown device {device!r}; choose from: {', '.join(DEVICE_CHOICES)}"
        )
    return device


def _ensure_device_available(device: str, torch_module: object | None = None) -> None:
    if device in {"auto", "cpu"}:
        return

    if torch_module is None:
        try:
            import torch as torch_module
        except ImportError as exc:
            raise ConversionError(
                f"{device.upper()} was requested with --device {device}, "
                "but PyTorch is not installed"
            ) from exc

    if device == "cuda":
        available = (
            getattr(getattr(torch_module, "version", None), "cuda", None)
            is not None
            and bool(torch_module.cuda.is_available())
        )
        label = "CUDA"
    elif device == "mps":
        mps = getattr(getattr(torch_module, "backends", None), "mps", None)
        available = bool(mps and mps.is_available())
        label = "MPS"
    else:
        xpu = getattr(torch_module, "xpu", None)
        available = bool(xpu and xpu.is_available())
        label = "XPU"

    if not available:
        raise ConversionError(
            f"{label} was requested with --device {device}, but this PyTorch "
            f"installation cannot access {label}"
        )


def _docling_device(device: str, accelerator_device: object) -> object:
    return getattr(accelerator_device, _normalize_device(device).upper())


# The default boundaries keep accidental or hostile inputs from taking a
# workstation down while still leaving room for ordinary business documents.
# They are deliberately configurable for trusted archival jobs.
DEFAULT_MAX_INPUT_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_PDF_PAGES = 500
DEFAULT_MAX_PAGE_PIXELS = 40_000_000
DEFAULT_MAX_TOTAL_PDF_PIXELS = 400_000_000
DEFAULT_MAX_OUTPUT_CHARS = 20_000_000
DEFAULT_MAX_BACKEND_SECONDS = 1_800
DEFAULT_MAX_ARCHIVE_MEMBERS = 10_000
DEFAULT_MAX_ARCHIVE_BYTES = 500 * 1024 * 1024

# These formats are ZIP containers.  Their compressed size is already bounded
# above, but their expanded size and member count need independent checks.
ZIP_CONTAINERS = {".docx", ".xlsx", ".pptx", ".epub"}

# These small, family-specific markers distinguish supported document packages
# from a generic ZIP renamed with a trusted suffix. Full schema validation stays
# with the format converter; this boundary only authorizes the intended route.
ZIP_CONTAINER_MARKERS = {
    ".docx": frozenset({"[Content_Types].xml", "word/document.xml"}),
    ".xlsx": frozenset({"[Content_Types].xml", "xl/workbook.xml"}),
    ".pptx": frozenset({"[Content_Types].xml", "ppt/presentation.xml"}),
    ".epub": frozenset({"mimetype", "META-INF/container.xml"}),
}

MARKITDOWN_PDF = "pdf"
MARKITDOWN_IMAGE = "image"


def _configure_docling_for_platform(
    options, device: str = "auto", platform: str | None = None
) -> None:
    """Keep optional Docling acceleration portable across supported platforms."""
    is_windows = (platform or os.name) == "nt"
    legacy_cuda = False
    if not is_windows and device in {"auto", "cuda"}:
        try:
            import torch
        except ImportError:
            pass
        else:
            # Docling's optional torch.compile path uses Triton, whose CUDA
            # backend rejects Pascal and older devices. Eager CUDA inference
            # still works, so disable compilation without disabling the GPU.
            legacy_cuda = (
                torch.version.cuda is not None
                and torch.cuda.is_available()
                and torch.cuda.get_device_capability() < (7, 0)
            )

    if not (is_windows or legacy_cuda):
        return

    # Recent Docling releases compile the layout model by default.  PyTorch's
    # Windows compiler path requires the optional Visual C++ build tools, so a
    # normal Python-only installation otherwise fails before its first image or
    # scanned PDF can be converted.  Older Docling releases do not expose this
    # option, hence the guarded lookup.
    layout = getattr(options, "layout_options", None)
    engine = getattr(layout, "engine_options", None)
    if hasattr(engine, "compile_model"):
        engine.compile_model = False


@dataclass(frozen=True)
class ConversionLimits:
    """Resource boundaries for one conversion or a CLI batch.

    Set a field to ``None`` only for inputs you trust.  The CLI exposes that
    choice as ``--unsafe-unlimited`` rather than making it the default.
    """

    max_input_bytes: int | None = DEFAULT_MAX_INPUT_BYTES
    max_files: int | None = DEFAULT_MAX_FILES
    max_pdf_pages: int | None = DEFAULT_MAX_PDF_PAGES
    max_page_pixels: int | None = DEFAULT_MAX_PAGE_PIXELS
    max_total_pdf_pixels: int | None = DEFAULT_MAX_TOTAL_PDF_PIXELS
    max_output_chars: int | None = DEFAULT_MAX_OUTPUT_CHARS
    max_archive_members: int | None = DEFAULT_MAX_ARCHIVE_MEMBERS
    max_archive_bytes: int | None = DEFAULT_MAX_ARCHIVE_BYTES
    max_backend_seconds: int | None = DEFAULT_MAX_BACKEND_SECONDS

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative or None")


DEFAULT_LIMITS = ConversionLimits()
UNLIMITED_LIMITS = ConversionLimits(
    max_input_bytes=None,
    max_files=None,
    max_pdf_pages=None,
    max_page_pixels=None,
    max_total_pdf_pixels=None,
    max_output_chars=None,
    max_backend_seconds=None,
    max_archive_members=None,
    max_archive_bytes=None,
)


@dataclass
class Result:
    markdown: str
    backend: str


_converters: dict[tuple, object] = {}

_HAS_SECURE_DIR_FD = (
    os.name != "nt"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open}.issubset(os.supports_dir_fd)
)


def _display_name(path: Path, name: str | None) -> str:
    """What to call this file in an error the user will read.

    The CLI hands backends a private snapshot rather than the caller's path
    (`cli._snapshot_input`), so `path.name` here is `d2md-input-<hex>.pdf`.
    Every message a user sees has to name the file they actually passed.
    """
    return name or path.name


def _check_limit(
    value: int | float,
    limit: int | None,
    label: str,
    name: str | Path,
) -> None:
    if limit is not None and value > limit:
        display = name.name if isinstance(name, Path) else name
        raise ConversionError(
            f"{label} limit exceeded: {display} has {value:,}; maximum is {limit:,}"
        )


def _file_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _is_link_or_reparse(details: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_no_link_components(path: Path) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            details = current.lstat()
        except OSError as exc:
            raise ConversionError(f"cannot inspect input {path.name}: {exc}") from exc
        if _is_link_or_reparse(details):
            raise ConversionError(f"symbolic link inputs are rejected: {path.name}")


def _open_verified_input_portable(path: Path) -> int:
    """Open a regular file after rejecting observed portable link components."""
    absolute = _lexical_absolute(path)
    _assert_no_link_components(absolute)
    try:
        before = absolute.lstat()
    except OSError as exc:
        raise ConversionError(f"cannot inspect input {path.name}: {exc}") from exc
    if _is_link_or_reparse(before):
        raise ConversionError(f"symbolic link inputs are rejected: {path.name}")
    if not stat.S_ISREG(before.st_mode):
        raise ConversionError(f"input must be a regular file: {path.name}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise ConversionError(f"cannot open input safely: {path.name}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        _assert_no_link_components(absolute)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != _file_identity(before)
        ):
            raise ConversionError(f"input changed while it was being opened: {path.name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_verified_input(path: Path) -> int:
    """Open a final regular file through stable no-follow directory handles."""
    if not _HAS_SECURE_DIR_FD:
        return _open_verified_input_portable(path)

    absolute = _lexical_absolute(path)
    components = absolute.parts[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise ConversionError(f"unsafe input path: {path.name}")

    directory_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        directory_descriptor = os.open(absolute.anchor, directory_flags)
    except OSError as exc:
        raise ConversionError(f"cannot open input safely: {path.name}: {exc}") from exc
    try:
        for component in components[:-1]:
            try:
                details = os.stat(
                    component,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ConversionError(
                    f"cannot inspect input safely: {path.name}: {exc}"
                ) from exc
            if _is_link_or_reparse(details):
                raise ConversionError(f"symbolic link inputs are rejected: {path.name}")
            if not stat.S_ISDIR(details.st_mode):
                raise ConversionError(f"input path component is not a directory: {path.name}")
            try:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise ConversionError(
                    f"cannot open input safely: {path.name}: {exc}"
                ) from exc
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        try:
            descriptor = os.open(
                components[-1],
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise ConversionError(
                f"cannot open input safely: {path.name}: {exc}"
            ) from exc
    finally:
        os.close(directory_descriptor)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ConversionError(f"input must be a regular file: {path.name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _snapshot_input(path: Path, limits: ConversionLimits):
    """Give every public conversion a bounded, immutable regular-file input."""
    descriptor = _open_verified_input(path)
    snapshot: Path | None = None
    try:
        size = os.fstat(descriptor).st_size
        _check_limit(size, limits.max_input_bytes, "input", path)
        with tempfile.NamedTemporaryFile(
            prefix="d2md-input-", suffix=path.suffix, delete=False
        ) as copy:
            # macOS commonly exposes its temporary directory through /var, a
            # symlink to /private/var.  This file is ours and mode 0600; use
            # its canonical path so the next secure open does not mistake the
            # platform's own alias for a caller-supplied link component.
            snapshot = Path(copy.name).resolve(strict=True)
            copied = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                copied += len(chunk)
                _check_limit(copied, limits.max_input_bytes, "input", path)
                copy.write(chunk)
        yield snapshot
    except OSError as exc:
        raise ConversionError(f"cannot snapshot input safely: {path.name}: {exc}") from exc
    finally:
        os.close(descriptor)
        if snapshot is not None:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass


def _check_input_size(
    path: Path, limits: ConversionLimits, name: str | None = None
) -> None:
    name = _display_name(path, name)
    try:
        details = path.lstat()
    except OSError as exc:
        raise ConversionError(f"cannot inspect input {name}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode):
        raise ConversionError(f"symbolic link inputs are rejected: {name}")
    if not stat.S_ISREG(details.st_mode):
        raise ConversionError(f"input must be a regular file: {name}")
    _check_limit(details.st_size, limits.max_input_bytes, "input", name)


def _page_pixels(page, scale: float = OCR_RENDER_SCALE) -> int:
    """Estimate the raster size before rendering a PDF page."""
    width, height = page.get_size()
    return ceil(width * scale) * ceil(height * scale)


def _validate_pdf(
    path: Path,
    limits: ConversionLimits,
    name: str | None = None,
    *,
    probe: bool = False,
) -> bool:
    """Reject over-budget PDFs and report whether a content probe parsed one."""
    name = _display_name(path, name)
    try:
        import pypdfium2
    except ImportError as exc:
        raise ConversionError(
            "PDF safety checks require pypdfium2; install the supported dependencies"
        ) from exc

    try:
        document = pypdfium2.PdfDocument(str(path))
    except Exception as exc:
        if probe:
            return False
        raise ConversionError(f"cannot inspect PDF safely: {name}: {exc}") from exc

    try:
        page_count = len(document)
        _check_limit(page_count, limits.max_pdf_pages, "PDF page", name)
        total_pixels = 0
        for index in range(page_count):
            page = document[index]
            try:
                pixels = _page_pixels(page)
                _check_limit(pixels, limits.max_page_pixels, "rendered page pixel", name)
                total_pixels += pixels
                _check_limit(
                    total_pixels,
                    limits.max_total_pdf_pixels,
                    "total PDF rendered pixel",
                    name,
                )
            finally:
                page.close()
    finally:
        document.close()
    return True


def _validate_image(
    path: Path,
    limits: ConversionLimits,
    name: str | None = None,
    *,
    probe: bool = False,
) -> bool:
    """Inspect image dimensions and report whether a content probe parsed one."""
    name = _display_name(path, name)
    try:
        from PIL import Image
    except ImportError as exc:
        if probe:
            return False
        raise ConversionError(
            "image safety checks require Pillow; install the supported dependencies"
        ) from exc

    try:
        with Image.open(path) as image:
            pixels = image.width * image.height
    except Image.DecompressionBombError as exc:
        raise ConversionError(f"cannot inspect image safely: {name}: {exc}") from exc
    except Exception as exc:
        if probe:
            return False
        raise ConversionError(f"cannot inspect image safely: {name}: {exc}") from exc
    _check_limit(pixels, limits.max_page_pixels, "image pixel", name)
    return True


_ZIP64_DISCOVERY_CHUNK_SIZE = 64 * 1024
# A ZIP has one end record; this tolerates incidental binary signatures while
# bounding attacker-controlled work before the central directory is inspected.
_MAX_ZIP64_RECORD_CANDIDATES = 4096


def _find_zip64_end_record(source, locator_offset: int, relative_offset: int):
    """Find exactly one bounded ZIP64 end record before its locator."""
    signature = b"PK\x06\x06"
    fixed_size = 56
    minimum_record_size = fixed_size - 12
    search_offset = relative_offset
    candidates = []
    candidate_count = 0

    while search_offset < locator_offset:
        primary_size = min(
            _ZIP64_DISCOVERY_CHUNK_SIZE, locator_offset - search_offset
        )
        source.seek(search_offset)
        chunk = source.read(
            min(locator_offset - search_offset, primary_size + fixed_size - 1)
        )
        searchable_size = min(len(chunk), primary_size + len(signature) - 1)
        candidate = chunk.find(signature, 0, searchable_size)
        while candidate >= 0 and candidate < primary_size:
            candidate_count += 1
            if candidate_count > _MAX_ZIP64_RECORD_CANDIDATES:
                raise struct.error("too many ZIP64 end-record candidates")
            if candidate + fixed_size <= len(chunk):
                record_offset = search_offset + candidate
                record = chunk[candidate : candidate + fixed_size]
                record_size = struct.unpack_from("<Q", record, 4)[0]
                if (
                    record_size >= minimum_record_size
                    and record_size <= locator_offset - record_offset - 12
                    and record_offset + record_size + 12 == locator_offset
                ):
                    candidates.append((record_offset, record))
                    if len(candidates) > 1:
                        raise struct.error("ambiguous ZIP64 end-record candidates")
            candidate = chunk.find(signature, candidate + 1, searchable_size)
        search_offset += primary_size

    return candidates[0] if candidates else None


def _validate_zip_member_count(
    path: Path, limit: int | None, name: str
) -> None:
    """Count bounded central-directory records without creating ZipInfo rows."""
    end_signature = b"PK\x05\x06"
    end_size = 22
    central_header_signature = b"PK\x01\x02"
    central_header_size = 46
    zip64_end_signature = b"PK\x06\x06"
    zip64_locator_signature = b"PK\x06\x07"
    zip64_locator_size = 20
    invalid_message = f"cannot inspect document archive safely: {name}"

    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            tail_size = min(size, end_size + 65_535)
            source.seek(size - tail_size)
            tail = source.read(tail_size)
            position = tail.rfind(end_signature)
            if position < 0 or position + end_size > len(tail):
                raise struct.error("missing ZIP end record")
            (
                _signature,
                disk_number,
                central_directory_disk,
                entries_on_disk,
                total_entries,
                central_directory_size,
                central_directory_offset,
                comment_size,
            ) = struct.unpack_from("<4s4H2LH", tail, position)
            if position + end_size + comment_size != len(tail):
                raise struct.error("invalid ZIP end record")
            if disk_number != 0 or central_directory_disk != 0:
                raise ConversionError(
                    f"multi-disk document archives are not supported: {name}"
                )

            end_offset = size - tail_size + position
            central_directory_end = end_offset
            zip64_prefix = None
            expected_entries = total_entries
            requires_zip64 = (
                entries_on_disk == 0xFFFF
                or total_entries == 0xFFFF
                or central_directory_size == 0xFFFFFFFF
                or central_directory_offset == 0xFFFFFFFF
            )
            locator_offset = end_offset - zip64_locator_size
            if locator_offset >= 0:
                source.seek(locator_offset)
                locator = source.read(zip64_locator_size)
                if len(locator) != zip64_locator_size:
                    raise struct.error("truncated ZIP64 locator")
                locator_signature, locator_disk, zip64_offset, disks = struct.unpack(
                    "<4sLQL", locator
                )
                if locator_signature == zip64_locator_signature:
                    if locator_disk != 0 or disks != 1:
                        raise ConversionError(
                            f"multi-disk document archives are not supported: {name}"
                        )
                    zip64_record = _find_zip64_end_record(
                        source, locator_offset, zip64_offset
                    )
                    if zip64_record is None:
                        raise struct.error("missing ZIP64 end record")
                    zip64_end_offset, record = zip64_record
                    (
                        record_signature,
                        _record_size,
                        _made_by,
                        _needed,
                        zip64_disk_number,
                        zip64_central_directory_disk,
                        zip64_entries_on_disk,
                        zip64_total_entries,
                        zip64_central_directory_size,
                        zip64_central_directory_offset,
                    ) = struct.unpack("<4sQ2H2L4Q", record)
                    if record_signature != zip64_end_signature:
                        raise struct.error("invalid ZIP64 end record")
                    if (
                        zip64_disk_number != 0
                        or zip64_central_directory_disk != 0
                    ):
                        raise ConversionError(
                            "multi-disk document archives are not supported: "
                            f"{name}"
                        )
                    zip64_prefix = zip64_end_offset - zip64_offset
                    if (
                        zip64_prefix < 0
                        or zip64_central_directory_size > zip64_end_offset
                    ):
                        raise struct.error("invalid ZIP64 end offset")
                    zip64_directory_start = (
                        zip64_end_offset - zip64_central_directory_size
                    )
                    if (
                        zip64_directory_start - zip64_central_directory_offset
                        != zip64_prefix
                    ):
                        raise struct.error("invalid ZIP64 central-directory offset")
                    central_directory_end = zip64_end_offset
                    entries_on_disk = zip64_entries_on_disk
                    total_entries = zip64_total_entries
                    expected_entries = total_entries
                    central_directory_size = zip64_central_directory_size
                    central_directory_offset = zip64_central_directory_offset

            if requires_zip64 and zip64_prefix is None:
                raise struct.error("missing ZIP64 end record")

            if expected_entries is not None:
                _check_limit(expected_entries, limit, "archive member", name)
            if expected_entries is not None and entries_on_disk != total_entries:
                raise struct.error("inconsistent ZIP member counts")
            if central_directory_size > central_directory_end:
                raise struct.error("invalid central-directory size")
            central_directory_start = (
                central_directory_end - central_directory_size
            )
            prefix = central_directory_start - central_directory_offset
            if prefix < 0 or (zip64_prefix is not None and prefix != zip64_prefix):
                raise struct.error("invalid central-directory offset")

            source.seek(central_directory_start)
            remaining = central_directory_size
            actual_entries = 0
            while remaining:
                if remaining < central_header_size:
                    raise struct.error("truncated central-directory header")
                header = source.read(central_header_size)
                if len(header) != central_header_size:
                    raise struct.error("truncated central-directory header")
                if header[:4] != central_header_signature:
                    raise struct.error("invalid central-directory signature")
                filename_size, extra_size, member_comment_size = struct.unpack_from(
                    "<3H", header, 28
                )
                variable_size = filename_size + extra_size + member_comment_size
                record_size = central_header_size + variable_size
                if record_size > remaining:
                    raise struct.error("central-directory record exceeds its bounds")
                actual_entries += 1
                _check_limit(actual_entries, limit, "archive member", name)
                source.seek(variable_size, os.SEEK_CUR)
                remaining -= record_size

            if expected_entries is not None and actual_entries != expected_entries:
                raise struct.error("inconsistent ZIP member count")
    except ConversionError:
        raise
    except (OSError, OverflowError, struct.error) as exc:
        raise ConversionError(invalid_message) from exc


def _validate_zip_container(
    path: Path, limits: ConversionLimits, name: str | None = None
) -> None:
    """Bound archive metadata before ZipFile creates one object per member."""
    name = _display_name(path, name)
    _validate_zip_member_count(path, limits.max_archive_members, name)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            _check_limit(
                sum(member.file_size for member in members),
                limits.max_archive_bytes,
                "archive expanded byte",
                name,
            )
    except ConversionError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConversionError(f"cannot inspect document archive safely: {name}: {exc}") from exc


def _validate_zip_document_type(path: Path, extension: str, name: str) -> None:
    """Require a supported ZIP package to match the family named by its suffix."""
    required = ZIP_CONTAINER_MARKERS[extension]
    mismatch = f"document archive does not match {extension}: {name}"
    try:
        with zipfile.ZipFile(path) as archive:
            counts: dict[str, int] = {}
            for member in archive.infolist():
                counts[member.filename] = counts.get(member.filename, 0) + 1
            if any(counts.get(member) != 1 for member in required):
                raise ConversionError(mismatch)
            if extension == ".epub":
                expected = b"application/epub+zip"
                with archive.open("mimetype") as member:
                    declared = member.read(len(expected) + 1)
                if declared != expected:
                    raise ConversionError(mismatch)
    except ConversionError:
        raise
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        raise ConversionError(
            f"cannot inspect document archive safely: {name}: {exc}"
        ) from exc


def _is_supported_image_header(header: bytes) -> bool:
    """Recognize supported raster formats from a bounded file header."""
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith(
            (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
        )
        or header.startswith(b"BM")
        or (
            len(header) >= 12
            and header.startswith(b"RIFF")
            and header[8:12] == b"WEBP"
        )
    )


def _probe_markitdown_content(
    path: Path, limits: ConversionLimits, name: str
) -> frozenset[str]:
    """Parser-confirm actual PDF/image content before enabling those fallbacks."""
    try:
        with path.open("rb") as source:
            # PDFium accepts a PDF marker beginning at byte 1,024. Include the
            # complete five-byte marker while keeping this content probe bounded.
            header = source.read(1_029)
    except OSError as exc:
        raise ConversionError(f"cannot inspect input {name}: {exc}") from exc

    validated: set[str] = set()
    if b"%PDF-" in header and _validate_pdf(path, limits, name, probe=True):
        validated.add(MARKITDOWN_PDF)
    if _is_supported_image_header(header) and _validate_image(
        path, limits, name, probe=True
    ):
        validated.add(MARKITDOWN_IMAGE)
    return frozenset(validated)


def _validate_input(
    path: Path, limits: ConversionLimits, name: str | None = None
) -> frozenset[str]:
    """Run cheap, format-aware guards before a heavy backend is invoked."""
    name = _display_name(path, name)
    _check_input_size(path, limits, name)
    extension = path.suffix.lower()

    # ZIP detection takes precedence because MarkItDown otherwise treats an
    # extensionless or renamed archive as a recursively expandable container.
    if zipfile.is_zipfile(path):
        _validate_zip_container(path, limits, name)
        if extension not in ZIP_CONTAINERS:
            raise ConversionError(
                f"generic ZIP archives are not supported: {name}; "
                "extract the document to convert and pass that file directly"
            )
        validated = _probe_markitdown_content(path, limits, name)
        _validate_zip_document_type(path, extension, name)
        return validated

    # Known formats remain fail-closed by their declared suffix.  In
    # particular, a corrupt Office container must still reach the ZIP
    # validator even when content detection cannot recognize it as a ZIP.
    if extension == ".pdf":
        _validate_pdf(path, limits, name)
    elif extension in PDFISH:
        _validate_image(path, limits, name)
    elif extension in ZIP_CONTAINERS:
        _validate_zip_container(path, limits, name)
    elif extension not in PLAIN:
        return _probe_markitdown_content(path, limits, name)
    return frozenset()


def _build_docling_converter(
    script: str | None,
    force_ocr: bool,
    device: str,
    ocr_enabled: bool,
) -> object:
    """Build one Docling converter for an explicit OCR state and device.

    The selected device is passed to Docling exactly: ``auto`` delegates the
    choice to Docling, while the explicit modes require that device. Converter
    caching is handled by ``_docling`` because model loading is paid per
    converter, not per file.

    When OCR is enabled, `script` decides which language the OCR engine is
    asked for. Getting it wrong is not a small error: Docling's own default can
    return a confident fragment from the wrong script.
    """
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import (
        DocumentConverter,
        ImageFormatOption,
        PdfFormatOption,
    )

    _ensure_device_available(device)
    opts = PdfPipelineOptions()
    opts.do_ocr = ocr_enabled
    opts.accelerator_options = AcceleratorOptions(
        num_threads=min(os.cpu_count() or 4, 4),
        device=_docling_device(device, AcceleratorDevice),
    )
    _configure_docling_for_platform(opts, device=device)

    if ocr_enabled and script:
        from docling.models.factories import get_ocr_factory

        from .ocr import ENGINE_SCRIPTS, engine_for

        engine = engine_for(script)
        kind = {"vision": "ocrmac", "rapidocr": "rapidocr"}[engine]
        ocr = get_ocr_factory(allow_external_plugins=False).create_options(
            kind=kind, lang=[ENGINE_SCRIPTS[engine][script]]
        )
        # Only when the text layer is known to be corrupt. Forcing OCR on a
        # born-digital page is actively harmful — it turns `f1c2a5e8` into
        # `f1¢2a5e8` and destroys table reading order (findings.md §3).
        #
        # It has to be `mode`. `force_full_page_ocr` is deprecated and the
        # model never reads it — `base_ocr_model` branches on `options.mode`
        # alone — so assigning the flag after construction silently does
        # nothing, which is how this shipped as a no-op the first time.
        if force_ocr:
            from docling.datamodel.pipeline_options import OcrMode

            ocr.mode = OcrMode.FULL_PAGE
        opts.ocr_options = ocr

    # Images need their own format option. Registering only PDF leaves .png
    # and friends on Docling's defaults — which is the English-only OCR this
    # whole change exists to remove.
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=opts),
        }
    )


def _docling(
    script: str | None = None,
    force_ocr: bool = False,
    device: str = "auto",
    ocr_enabled: bool = False,
) -> object:
    normalized = _normalize_device(device)
    key = (script, force_ocr, normalized, ocr_enabled)
    if key not in _converters:
        _converters[key] = _build_docling_converter(
            script, force_ocr, normalized, ocr_enabled
        )
    return _converters[key]


def _docling_pdf_pages(document: object) -> list[str]:
    from docling_core.transforms.serializer.markdown import (
        MarkdownDocSerializer,
        MarkdownParams,
    )

    page_sections = [[] for _ in range(max(document.pages, default=0))]
    serializer = MarkdownDocSerializer(
        doc=document,
        params=MarkdownParams(page_break_placeholder=DOCLING_PAGE_BREAK),
    )

    def section_text(text: str, page_numbers: list[int]) -> str:
        if len(page_numbers) > 1:
            return f"{format_pdf_content_span(page_numbers)}\n\n{text}"
        return text

    for part in serializer.get_parts():
        page_numbers = list(
            dict.fromkeys(
                provenance.page_no
                for span in part.spans
                for provenance in span.item.prov
            )
        )
        if not page_numbers:
            continue

        page_number = page_numbers[0]
        page_breaks = list(serializer._get_page_breaks(part.text))
        if not page_breaks:
            page_sections[page_number - 1].append(
                section_text(part.text, page_numbers)
            )
            continue

        start = 0
        segments = []
        for page_break, previous_page, next_page in page_breaks:
            end = part.text.index(page_break, start)
            segment_pages = [
                page
                for page in page_numbers
                if previous_page <= page < next_page
            ]
            segments.append((previous_page, part.text[start:end], segment_pages))
            page_number = next_page
            start = end + len(page_break)
        segment_pages = [page for page in page_numbers if page >= page_number]
        segments.append((page_number, part.text[start:], segment_pages))

        if all(
            text.strip() and page_number in segment_pages
            for page_number, text, segment_pages in segments
        ):
            for page_number, text, segment_pages in segments:
                page_sections[page_number - 1].append(
                    section_text(text, segment_pages)
                )
            continue

        text = part.text
        for page_break, _, _ in page_breaks:
            text = text.replace(page_break, "")
        page_sections[page_numbers[0] - 1].append(
            section_text(text, page_numbers)
        )

    return ["\n\n".join(section) for section in page_sections]


def _via_docling(
    path: Path,
    script: str | None = None,
    force_ocr: bool = False,
    device: str = "auto",
    ocr_enabled: bool = False,
) -> str:
    # Keep Docling imports lazy. Its accelerator utility can still reject an
    # explicitly requested device after our PyTorch preflight, and that error
    # must not be mistaken for a recoverable parser failure by the auto route.
    disable_onnx_telemetry()
    from docling.exceptions import AcceleratorDeviceNotAvailableError

    try:
        document = _docling(
            script,
            force_ocr,
            device=device,
            ocr_enabled=ocr_enabled,
        ).convert(str(path)).document
        if path.suffix.lower() == ".pdf":
            return format_pdf_pages(_docling_pdf_pages(document))
        return document.export_to_markdown()
    except AcceleratorDeviceNotAvailableError as exc:
        raise ConversionError(str(exc)) from exc


def _build_markitdown(validated_formats: frozenset[str]):
    """Build MarkItDown without its recursive generic-ZIP converter."""
    disable_onnx_telemetry()
    from markitdown import MarkItDown
    from markitdown.converters import (
        AudioConverter,
        BingSerpConverter,
        CsvConverter,
        DocxConverter,
        EpubConverter,
        HtmlConverter,
        ImageConverter,
        IpynbConverter,
        OutlookMsgConverter,
        PdfConverter,
        PlainTextConverter,
        PptxConverter,
        RssConverter,
        WikipediaConverter,
        XlsConverter,
        XlsxConverter,
        YouTubeConverter,
    )
    from markitdown import DocumentConverterResult
    import pandas as pd

    def convert_spreadsheet(file_stream, engine, html_converter, **kwargs):
        sheets = pd.read_excel(
            file_stream,
            sheet_name=None,
            engine=engine,
            keep_default_na=False,
        )
        sections = []
        for sheet_name, sheet in sheets.items():
            html = sheet.to_html(index=False, na_rep="")
            table = html_converter.convert_string(html, **kwargs).markdown.strip()
            sections.append(f"## {sheet_name}\n{table}")
        return DocumentConverterResult(markdown="\n\n".join(sections))

    class SourcePreservingXlsxConverter(XlsxConverter):
        def convert(self, file_stream, stream_info, **kwargs):
            return convert_spreadsheet(
                file_stream, "openpyxl", self._html_converter, **kwargs
            )

    class SourcePreservingXlsConverter(XlsConverter):
        def convert(self, file_stream, stream_info, **kwargs):
            return convert_spreadsheet(
                file_stream, "xlrd", self._html_converter, **kwargs
            )

    converter = MarkItDown(enable_builtins=False)
    converter.register_converter(PlainTextConverter(), priority=10.0)
    converter.register_converter(HtmlConverter(), priority=10.0)
    specific = [
        RssConverter(),
        WikipediaConverter(),
        YouTubeConverter(),
        BingSerpConverter(),
        DocxConverter(),
        SourcePreservingXlsxConverter(),
        SourcePreservingXlsConverter(),
        PptxConverter(),
        AudioConverter(),
    ]
    if MARKITDOWN_IMAGE in validated_formats:
        specific.append(ImageConverter())
    specific.append(IpynbConverter())
    if MARKITDOWN_PDF in validated_formats:
        specific.append(PdfConverter())
    specific.extend((OutlookMsgConverter(), EpubConverter(), CsvConverter()))
    for document_converter in specific:
        converter.register_converter(document_converter)
    return converter


def _via_markitdown(
    path: Path, validated_formats: frozenset[str] = frozenset()
) -> str:
    return _build_markitdown(validated_formats).convert(str(path)).text_content


def _run_isolated_backend(
    backend: str,
    path: Path,
    *,
    max_output_chars: int | None,
    timeout_seconds: int | None,
    validated_formats: frozenset[str] = frozenset(),
    script: str | None = None,
    force_ocr: bool = False,
    device: str = "auto",
    ocr_enabled: bool = False,
) -> str:
    """Run a heavy converter outside the long-lived caller process."""
    from ._backend_process import run_isolated_backend

    return run_isolated_backend(
        backend,
        path,
        max_output_chars=max_output_chars,
        timeout_seconds=timeout_seconds,
        validated_formats=validated_formats,
        script=script,
        force_ocr=force_ocr,
        device=device,
        ocr_enabled=ocr_enabled,
    )


def _via_pypdfium2(
    path: Path,
    limits: ConversionLimits = DEFAULT_LIMITS,
    name: str | None = None,
) -> list[str]:
    """Read the PDF's text layer, one string per page. No structure, no OCR.

    This is the default lightweight PDF path. What it gives up is table
    reconstruction, heading levels and OCR, so `convert` checks the result
    before keeping it.
    """
    import pypdfium2

    name = _display_name(path, name)
    pages: list[str] = []
    total_chars = 0
    doc = pypdfium2.PdfDocument(str(path))
    try:
        for index, page in enumerate(doc, 1):
            _check_limit(index, limits.max_pdf_pages, "PDF page", name)
            textpage = page.get_textpage()
            try:
                page_chars = textpage.count_chars()
                _check_limit(
                    total_chars + page_chars,
                    limits.max_output_chars,
                    "extracted text",
                    name,
                )
                text = textpage.get_text_range(index=0, count=page_chars).replace(
                    "\r\n", "\n"
                )
                total_chars += len(text)
                pages.append(text)
            finally:
                textpage.close()
                page.close()
    finally:
        doc.close()
    return pages


def _fast_text_is_trustworthy(pages: list[str]) -> bool:
    """Decide whether pypdfium2's output can stand in for Docling's.

    Two ways it cannot: the file is a scan with no text layer, or the text
    layer exists but its Thai came out stripped of marks. Neither condition
    raises, so both have to be tested before direct text is accepted.
    """
    if not pages:
        return False

    empty = sum(len(p.strip()) < EMPTY_PAGE_CHARS for p in pages)
    if empty / len(pages) > SCAN_PAGE_FRACTION:
        return False

    return not thai_looks_damaged("\n".join(pages))


def backend_for(path: Path, *, docling: bool = False) -> str:
    ext = path.suffix.lower()
    if ext in PLAIN:
        return "plain"
    if ext in OFFICE:
        return "markitdown"
    if ext == ".pdf":
        return "docling" if docling else "pypdfium2"
    if ext in IMAGES:
        return "docling" if docling else "ocr"
    return "markitdown"


#: How many pages to try before giving up on detecting a script. A blank cover
#: page is common and must not decide the whole document; three is enough to
#: get past one without paying for a scan of the entire file.
DETECT_PAGES = 3


def _sample_images(
    path: Path,
    limit: int = DETECT_PAGES,
    scale: float = OCR_RENDER_SCALE,
    limits: ConversionLimits = DEFAULT_LIMITS,
    name: str | None = None,
):
    """Yield page images to detect from. Handles images as well as PDFs.

    `.png` and friends are in PDFISH and reach here too, and pdfium cannot open
    them — it raises `PdfiumError: Data format error`. Sending every image
    through that path failed every image file in the folder, which is a
    regression this function exists to not repeat.
    """
    name = _display_name(path, name)
    if path.suffix.lower() != ".pdf":
        from PIL import Image

        try:
            with Image.open(path) as img:
                _check_limit(
                    img.width * img.height,
                    limits.max_page_pixels,
                    "image pixel",
                    name,
                )
                yield img.convert("RGB")
        except ConversionError:
            raise
        except Exception:
            return
        return

    import pypdfium2

    try:
        doc = pypdfium2.PdfDocument(str(path))
    except Exception:
        return
    try:
        for index in range(min(len(doc), limit)):
            page = doc[index]
            try:
                _check_limit(
                    _page_pixels(page, scale),
                    limits.max_page_pixels,
                    "rendered page pixel",
                    name,
                )
                yield page.render(scale=scale).to_pil()
            finally:
                page.close()
    finally:
        doc.close()


def script_of(
    path: Path,
    lang: str | None = None,
    limits: ConversionLimits = DEFAULT_LIMITS,
    name: str | None = None,
) -> str | None:
    """Which script this document is in, or None if it cannot be determined.

    Detection costs one OCR pass per candidate script, so it runs on the first
    few pages rather than every page, and not at all when `lang` says what the
    answer is.

    A blank page or unreadable sample returns ``None``. Direct OCR then asks
    the caller for ``--lang``; Docling may retain its own default OCR settings.
    """
    if lang:
        return lang

    from .ocr import NoEngineFor, detect_script

    for image in _sample_images(path, limits=limits, name=name):
        try:
            return detect_script(image)
        except NoEngineFor:
            continue  # blank page, or nothing installed — try the next one
    return None


def _restore_source_name(error: ConversionError, snapshot: Path, source: Path) -> ConversionError:
    """Keep private snapshot filenames out of a caller-facing error."""
    message = str(error).replace(str(snapshot), source.name)
    return ConversionError(message.replace(snapshot.name, source.name))


def _convert_snapshot(
    input_path: Path,
    source_path: Path,
    *,
    backend: str,
    active_limits: ConversionLimits,
    lang: str | None,
    selected_device: str,
    ocr: bool,
) -> tuple[str, str]:
    """Run preflight and conversion using only one verified input snapshot."""
    validated_formats = _validate_input(input_path, active_limits)

    if backend == "plain":
        return (
            read_text(
                input_path,
                max_bytes=active_limits.max_input_bytes,
                max_chars=active_limits.max_output_chars,
            ),
            backend,
        )
    if backend == "markitdown":
        return (
            _run_isolated_backend(
                backend,
                input_path,
                max_output_chars=active_limits.max_output_chars,
                timeout_seconds=active_limits.max_backend_seconds,
                validated_formats=validated_formats,
                script=None,
                force_ocr=False,
                device="auto",
                ocr_enabled=False,
            ),
            backend,
        )
    if backend == "pypdfium2":
        try:
            pages = _via_pypdfium2(input_path, limits=active_limits)
        except ConversionError:
            raise
        except Exception:
            pages = []
        if _fast_text_is_trustworthy(pages):
            return format_pdf_pages(pages), backend
        if not ocr:
            raise ConversionError(
                f"{source_path.name} needs OCR; run "
                f"{install_command('ocr', force=True)} and rerun with --ocr"
            )
        script = script_of(input_path, lang, limits=active_limits)
        if script is None:
            raise ConversionError("could not determine the OCR script; pass --lang SCRIPT")
        return convert_with_ocr(input_path, script, active_limits)
    if backend == "ocr":
        script = script_of(input_path, lang, limits=active_limits)
        if script is None:
            raise ConversionError("could not determine the OCR script; pass --lang SCRIPT")
        return convert_with_ocr(input_path, script, active_limits)

    assert backend == "docling"
    script = script_of(input_path, lang, limits=active_limits) if ocr else None
    force_ocr = False
    if ocr and source_path.suffix.lower() == ".pdf":
        try:
            pages = _via_pypdfium2(input_path, limits=active_limits)
        except Exception:
            pages = []
        force_ocr = bool(pages) and thai_looks_damaged("\n".join(pages))
    markdown = _run_isolated_backend(
        "docling",
        input_path,
        max_output_chars=active_limits.max_output_chars,
        timeout_seconds=active_limits.max_backend_seconds,
        validated_formats=validated_formats,
        script=script,
        force_ocr=force_ocr,
        device=selected_device,
        ocr_enabled=ocr,
    )
    return markdown, "docling+ocr" if ocr else "docling"


def convert(
    path: Path,
    fast: bool | None = None,
    lang: str | None = None,
    limits: ConversionLimits | None = None,
    device: str = "auto",
    *,
    ocr: bool = False,
    docling: bool = False,
    display_name: str | None = None,
) -> Result:
    """Convert one file through only the capabilities explicitly requested."""
    if fast is not None:
        warnings.warn(
            "fast is deprecated; PDF text extraction is now the default",
            DeprecationWarning,
            stacklevel=2,
        )

    selected_device = _normalize_device(device)
    name = _display_name(path, display_name)
    if lang is not None and not ocr:
        raise ConversionError("lang requires ocr=True")
    if selected_device != "auto" and not docling:
        raise ConversionError("device selection requires docling=True")
    if ocr:
        ensure_ocr_available()
    if docling:
        ensure_docling_available()

    active_limits = DEFAULT_LIMITS if limits is None else limits
    backend = backend_for(path, docling=docling)
    if backend == "ocr" and not ocr:
        raise ConversionError(
            f"{name} needs OCR; run {install_command('ocr', force=True)} "
            "and rerun with --ocr"
        )

    with _snapshot_input(path, active_limits) as input_path:
        try:
            markdown, backend = _convert_snapshot(
                input_path,
                path,
                backend=backend,
                active_limits=active_limits,
                lang=lang,
                selected_device=selected_device,
                ocr=ocr,
            )
        except ConversionError as error:
            restored = _restore_source_name(error, input_path, path)
            if display_name is not None:
                restored = _restore_source_name(restored, path, Path(display_name))
            raise restored from error

    _check_limit(len(markdown), active_limits.max_output_chars, "output", name)
    content = pdf_content(markdown) if path.suffix.lower() == ".pdf" else markdown
    content_chars = len(content.strip())
    if content_chars < MIN_CHARS:
        raise ConversionError(
            f"{backend} produced no usable text "
            f"({content_chars} chars) — the file is probably a scan "
            f"the backend cannot read, or is corrupt"
        )
    return Result(markdown=markdown, backend=backend)
