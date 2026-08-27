"""Input and output safety shared by the legacy benchmark scripts."""

from __future__ import annotations

from pathlib import Path
import stat
import unicodedata


_TABLE_ENTITIES = {
    "&": "&amp;",
    "\\": "&#92;",
    "|": "&#124;",
    "<": "&lt;",
    ">": "&gt;",
    "[": "&#91;",
    "]": "&#93;",
    "!": "&#33;",
    "(": "&#40;",
    ")": "&#41;",
    "`": "&#96;",
}


def display_text(value: object, *, limit: int | None = None) -> str:
    """Render untrusted benchmark output without terminal control characters."""
    text = str(value)
    if limit is not None:
        text = text[:limit]

    rendered: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C") or not char.isprintable():
            codepoint = ord(char)
            if codepoint <= 0xFF:
                rendered.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                rendered.append(f"\\u{codepoint:04x}")
            else:
                rendered.append(f"\\U{codepoint:08x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def table_text(value: object) -> str:
    """Encode terminal-safe text for Markdown-shaped benchmark tables."""
    return "".join(_TABLE_ENTITIES.get(char, char) for char in display_text(value))


def load_result(path: Path) -> dict:
    """Load the result fields consumed as legacy table labels."""
    if __package__:
        from bench.matrix import MAX_RAW_RESULT_BYTES, load_bounded_strict_json
    else:
        from matrix import MAX_RAW_RESULT_BYTES, load_bounded_strict_json

    try:
        data = load_bounded_strict_json(path, max_bytes=MAX_RAW_RESULT_BYTES)
    except ValueError as error:
        raise ValueError(display_text(error)) from None
    if not isinstance(data, dict):
        raise ValueError("legacy result must be a JSON object")
    if not isinstance(data.get("engine"), str):
        raise ValueError("legacy result engine must be a string")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("legacy result rows must be a list")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"legacy result row {index} must be an object")
        if not isinstance(row.get("variant"), str):
            raise ValueError(f"legacy result row {index} variant must be a string")
    return data


def truth_file(corpus: Path, identifier: object) -> Path:
    """Resolve one legacy corpus stem to a regular truth file."""
    if (
        not isinstance(identifier, str)
        or identifier in {"", ".", ".."}
        or Path(identifier).is_absolute()
        or any(separator in identifier for separator in ("/", "\\", ":"))
        or any(
            unicodedata.category(char).startswith("C") or not char.isprintable()
            for char in identifier
        )
    ):
        raise ValueError("truth identifier is not a safe corpus stem")

    try:
        truth_root = (corpus / "truth").resolve(strict=True)
        candidate = truth_root / f"{identifier}.txt"
        details = candidate.lstat()
    except OSError as error:
        raise ValueError("truth file is unavailable") from error

    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError("truth file must be a regular non-symlink .txt file")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(truth_root)
    except (OSError, ValueError) as error:
        raise ValueError("truth file must stay inside the resolved truth root") from error
    if resolved.suffix != ".txt":
        raise ValueError("truth file must be a regular non-symlink .txt file")
    return resolved
