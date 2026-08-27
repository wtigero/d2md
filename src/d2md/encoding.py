"""Text decoding that gets Thai right.

Legacy Thai text files are still commonly TIS-620 / CP874. Charset detectors
guess these badly: on a 27-byte Thai file, `charset_normalizer` reports CP949
(Korean) with high confidence and returns `럽姦봉虜壘력 cp874 백斷丹磯` — no
exception, no warning, just wrong text. So we try the encodings that actually
matter first and only fall back to detection.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path

from .errors import ConversionError

THAI_START, THAI_END = 0x0E01, 0x0E5B
THAI_MIN_RATIO = 0.15

# Consonants only (ก–ฮ), deliberately excluding vowels and tone marks: the
# damage we are looking for is the *loss* of those marks, so they cannot be
# part of the measure of how much Thai a document contains.
THAI_CONSONANT_START, THAI_CONSONANT_END = 0x0E01, 0x0E2E

SARA_AM = "ำ"  # ำ

#: Below this many Thai consonants the `ำ` test means nothing — an English
#: document with a few dozen incidental Thai characters false-positives.
DAMAGE_MIN_CONSONANTS = 400
_DECODE_CHUNK_BYTES = 64 * 1024
_DETECTION_SAMPLE_BYTES = 64 * 1024
_DETECTION_SAMPLE_CHUNKS = 8


def thai_ratio(text: str) -> float:
    """Fraction of characters in the Thai Unicode block."""
    if not text:
        return 0.0
    return sum(THAI_START <= ord(c) <= THAI_END for c in text) / len(text)


def thai_consonant_count(text: str) -> int:
    return sum(THAI_CONSONANT_START <= ord(c) <= THAI_CONSONANT_END for c in text)


def thai_looks_damaged(text: str) -> bool:
    """True if this looks like Thai that lost its marks on the way out of a PDF.

    A PDF exported from Word often carries a broken ToUnicode CMap: the page
    renders fine but extraction drops tone marks and turns `ำ` into a space.
    The obvious detector — the ratio of tone marks to consonants — does not
    work, because the loss is partial; two known-broken files pass it at 0.045
    and 0.086. Counting `ำ` (U+0E33) does separate cleanly:

        broken   thai=5003  ำ=0    ํ=0
        broken   thai=6592  ำ=0    ํ=73    (decomposed to a bare nikhahit)
        healthy  thai=3862  ำ=316  ํ=0
        healthy  thai=2476  ำ=28   ํ=0

    Thai prose of any length without a single `ำ` is effectively impossible —
    คำ ทำ จำ น้ำ สำ are too common to all be absent.

    Used only to decide whether the fast path's text can be trusted. Docling
    reads both cases correctly, so nothing downstream depends on this being
    right; a false positive costs a slower conversion, nothing more.
    """
    if thai_consonant_count(text) < DAMAGE_MIN_CONSONANTS:
        return False
    return SARA_AM not in text


def _decode_with_limit(
    raw: bytes,
    encoding: str,
    max_chars: int | None,
    name: str,
    *,
    errors: str = "strict",
) -> str:
    """Decode incrementally so one codec call cannot bypass the output limit."""
    if max_chars is None:
        return raw.decode(encoding, errors=errors)

    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    pieces: list[str] = []
    total_chars = 0
    offset = 0
    while offset < len(raw):
        remaining = max_chars - total_chars
        chunk_size = min(_DECODE_CHUNK_BYTES, max(1, remaining + 1))
        piece = decoder.decode(raw[offset : offset + chunk_size], final=False)
        offset += chunk_size
        total_chars += len(piece)
        if total_chars > max_chars:
            raise ConversionError(
                f"output limit exceeded: {name} has more than "
                f"{max_chars:,} characters; maximum is {max_chars:,}"
            )
        pieces.append(piece)

    piece = decoder.decode(b"", final=True)
    total_chars += len(piece)
    if total_chars > max_chars:
        raise ConversionError(
            f"output limit exceeded: {name} has more than "
            f"{max_chars:,} characters; maximum is {max_chars:,}"
        )
    pieces.append(piece)
    return "".join(pieces)


def _detection_sample(raw: bytes) -> bytes:
    """Bound detector work while representing the whole input, not only its head."""
    if len(raw) <= _DETECTION_SAMPLE_BYTES:
        return raw

    chunk_size = _DETECTION_SAMPLE_BYTES // _DETECTION_SAMPLE_CHUNKS
    last_start = len(raw) - chunk_size
    starts = (
        round(index * last_start / (_DETECTION_SAMPLE_CHUNKS - 1))
        for index in range(_DETECTION_SAMPLE_CHUNKS)
    )
    return b"".join(raw[start : start + chunk_size] for start in starts)


def decode(
    raw: bytes, max_chars: int | None = None, name: str = "input"
) -> tuple[str, str]:
    """Decode bytes to text. Returns (text, encoding_used).

    Order matters:
      1. UTF-8 — unambiguous enough that a clean decode is trustworthy.
      2. CP874 — accepted only if the result actually looks like Thai, which
         is what stops us from mangling a genuinely Korean or Cyrillic file.
      3. Detection library — last resort.
      4. CP874 with replacement — never raises.
    """
    # utf-8-sig first: it strips a BOM when present and behaves exactly like
    # utf-8 when absent. The other order leaves a stray ﻿ at the start.
    try:
        return _decode_with_limit(raw, "utf-8-sig", max_chars, name), "utf-8"
    except UnicodeDecodeError:
        pass

    try:
        text = _decode_with_limit(raw, "cp874", max_chars, name)
        if thai_ratio(text) > THAI_MIN_RATIO:
            return text, "cp874"
    except UnicodeDecodeError:
        pass

    try:
        from charset_normalizer import from_bytes

        detection_sample = raw if max_chars is None else _detection_sample(raw)
        best = from_bytes(detection_sample).best()
        if best is not None:
            if max_chars is None:
                return str(best), best.encoding
            return (
                _decode_with_limit(
                    raw,
                    best.encoding,
                    max_chars,
                    name,
                    errors="replace",
                ),
                best.encoding,
            )
    except ImportError:
        pass

    return (
        _decode_with_limit(
            raw, "cp874", max_chars, name, errors="replace"
        ),
        "cp874/replace",
    )


def read_text(
    path: Path,
    max_bytes: int | None = None,
    max_chars: int | None = None,
    name: str | None = None,
) -> str:
    """Read a text input without allowing an unbounded allocation.

    Reading one byte past the limit also protects against a file that grows
    after its caller's size check.  ``None`` is reserved for the explicit
    trusted-input override exposed by the CLI.

    ``name`` is what errors call the file; the CLI passes the name the user
    typed, because ``path`` by then is a private snapshot of it.
    """
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative or None")
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must be non-negative or None")

    name = name or path.name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConversionError(f"cannot read input safely: {name}: {exc}") from exc

    with os.fdopen(descriptor, "rb") as source:
        raw = source.read() if max_bytes is None else source.read(max_bytes + 1)

    if max_bytes is not None and len(raw) > max_bytes:
        raise ConversionError(
            f"input limit exceeded: {name} is larger than {max_bytes:,} bytes"
        )
    return decode(raw, max_chars=max_chars, name=name)[0]
