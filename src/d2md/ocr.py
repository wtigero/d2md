"""Choosing an OCR engine, and telling it which script it is looking at.

Every OCR engine here has to be told the script in advance, and every one of
them returns confident nonsense when told the wrong one. Apple Vision asked for
English on a Thai page returns the eleven ASCII characters and no error at all;
that is not a hypothetical, it is what `d2md` shipped. So something upstream
has to choose, and the choice cannot come from the engine.

The unit of choice is the **script**, not the language. Measured in
`docs/ocr.md`: reading a German page as Vietnamese, or Traditional Chinese as
Simplified, changes nothing at all — every Latin document scored identically
under English, German, Vietnamese and French settings. What does matter is
crossing a script boundary, and the sharpest of those is kana: Japanese read as
Chinese scores 0.627 where the right script scores 0.000.

That is why this module knows about seven buckets rather than thirty languages.
"""

from __future__ import annotations

from importlib.util import find_spec
import platform
import unicodedata
from dataclasses import dataclass

from ._onnx import disable_onnx_telemetry
from .errors import ConversionError

#: Ranges that identify a script. Han is deliberately shared between Japanese
#: and Chinese — it is kana that break the tie, weighted separately below.
BLOCKS: dict[str, list[tuple[int, int]]] = {
    "latin": [(0x0041, 0x005A), (0x0061, 0x007A)],
    "thai": [(0x0E01, 0x0E5B)],
    "japanese": [(0x3040, 0x30FF), (0x4E00, 0x9FFF)],
    "chinese": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "korean": [(0x1100, 0x11FF), (0xAC00, 0xD7AF), (0x3130, 0x318F)],
    "cyrillic": [(0x0400, 0x04FF)],
    "arabic": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "devanagari": [(0x0900, 0x097F)],
}

KANA = [(0x3040, 0x309F), (0x30A0, 0x30FF)]

#: What each engine calls each script, and `None` where it has no model.
#: Worst-case CER for every one of these is recorded in `docs/ocr.md`; a script
#: an engine reads badly is listed as unsupported rather than as an option,
#: which is why RapidOCR has no Korean here despite shipping a Korean model.
ENGINE_SCRIPTS: dict[str, dict[str, str | None]] = {
    "vision": {
        "latin": "en-US", "thai": "th-TH", "japanese": "ja-JP",
        "chinese": "zh-Hans", "korean": "ko-KR", "cyrillic": "ru-RU",
        "arabic": "ar-SA", "devanagari": None,
    },
    "rapidocr": {
        "latin": "en", "chinese": "ch", "japanese": "japan",
        # Measured unusable and excluded on purpose: Korean 0.730, Thai 0.255,
        # Cyrillic 0.116, Arabic 0.187, Devanagari 0.391. See docs/ocr.md.
        "thai": None, "korean": None, "cyrillic": None,
        "arabic": None, "devanagari": None,
    },
}


class NoEngineFor(ConversionError):
    """Raised when nothing installed can read the script on the page.

    Deliberately loud. The failure this replaces was silent: a scanned Korean
    page OCR'd as English returned nothing and exited zero.
    """


@dataclass
class Reading:
    """One engine's attempt at one page under one script assumption."""

    script: str
    text: str
    confidence: float

    @property
    def score(self) -> float:
        return self.confidence * script_fit(self.text, self.script)


def _in(ch: str, ranges: list[tuple[int, int]]) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in ranges)


def significant(text: str) -> str:
    """Drop digits, punctuation and whitespace.

    Business documents carry the same policy and phone numbers in every
    language, and those characters are identical in all of them. Leaving them
    in dilutes the signal toward whichever reading is longest.
    """
    return "".join(
        c for c in text
        if not c.isspace() and not c.isdigit()
        and unicodedata.category(c)[0] not in {"P", "S"}
    )


def script_fit(text: str, script: str) -> float:
    """How much of `text` is written in `script`, from 0.0 to 1.0.

    Latin is excluded from the denominator for every non-Latin script, because
    a Thai quotation quoting `Cloud Storage Premium` is still a Thai document.
    Counting those characters against Thai is what made an earlier version pick
    English on a page that was 38% English by character count and entirely Thai
    in meaning.
    """
    body = significant(text)
    if not body:
        return 0.0

    latin = sum(_in(c, BLOCKS["latin"]) for c in body)
    if script == "latin":
        return latin / len(body)

    distinguishing = len(body) - latin
    if not distinguishing:
        return 0.0

    hit = sum(_in(c, BLOCKS[script]) for c in body) / distinguishing

    # Han is shared. A Japanese page almost always carries kana; a Chinese one
    # never does. Without this the two score identically on Han-only text.
    if script == "japanese" and not any(_in(c, KANA) for c in body):
        hit *= 0.5
    if script == "chinese" and any(_in(c, KANA) for c in body):
        hit *= 0.3

    return min(hit, 1.0)


def choose(readings: list[Reading]) -> Reading | None:
    """Pick the reading that was asked the right question.

    Score is confidence weighted by script fit and by how much text came back,
    measured against the best candidate on the same page. An engine asked for
    the wrong script does not fail loudly — it returns the fragment it could
    read, confidently — so the reading that recovered the most text is the one
    that was asked correctly.
    """
    usable = [r for r in readings if r.text.strip()]
    if not usable:
        return None
    longest = max(len(significant(r.text)) for r in usable) or 1
    return max(
        usable,
        key=lambda r: r.score * (len(significant(r.text)) / longest),
    )


def available_engines() -> list[str]:
    """Engines that can actually run here, best first.

    Apple Vision comes first wherever it exists: it reads every script it
    supports at 0.017 or better, in under half a second a page, with nothing to
    download. RapidOCR is the portable fallback and is limited on purpose to
    the three scripts it reads well.
    """
    found = []
    if platform.system() == "Darwin":
        try:
            import ocrmac  # noqa: F401

            found.append("vision")
        except ImportError:
            pass
    if find_spec("rapidocr") is not None:
        disable_onnx_telemetry()
        try:
            import rapidocr  # noqa: F401

            found.append("rapidocr")
        except ImportError:
            pass
    return found


def engine_for(script: str, engines: list[str] | None = None) -> str:
    """The best available engine for `script`. Raises `NoEngineFor` if none."""
    for name in engines if engines is not None else available_engines():
        if ENGINE_SCRIPTS.get(name, {}).get(script):
            return name
    raise NoEngineFor(
        f"no installed OCR engine reads {script!r}. "
        f"Available: {', '.join(available_engines()) or 'none'}."
    )


def supported_scripts(engines: list[str] | None = None) -> set[str]:
    names = engines if engines is not None else available_engines()
    return {
        script
        for name in names
        for script, code in ENGINE_SCRIPTS.get(name, {}).items()
        if code
    }


def _read_vision(image, script: str) -> Reading:
    from ocrmac import ocrmac

    code = ENGINE_SCRIPTS["vision"][script]
    # `language_preference` is an ordered preference, not a set: asked for
    # ["en-US", "th-TH"] on a Thai page Vision returns eleven characters at
    # confidence 0.30, and for ["th-TH", "en-US"] all 313 at 1.00. One script
    # per call is the only way to get a comparable answer.
    found = ocrmac.OCR(
        image, language_preference=[code], recognition_level="accurate"
    ).recognize()
    if not found:
        return Reading(script, "", 0.0)
    return Reading(
        script,
        " ".join(r[0] for r in found),
        sum(r[1] for r in found) / len(found),
    )


_rapid_engines: dict[str, object] = {}


def _read_rapidocr(image, script: str) -> Reading:
    disable_onnx_telemetry()
    import numpy as np
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    # PP-OCRv6's `ch` model is not a Chinese model: measured at 0.000 on
    # English, Japanese, Simplified and Traditional Chinese alike, and it beats
    # every per-language alternative on all four. Latin keeps the v5 model,
    # which reads it at 0.000 without pulling in the CJK charset.
    spec = {
        "latin": (LangRec.EN, OCRVersion.PPOCRV5, ModelType.MOBILE),
        "chinese": (LangRec.CH, OCRVersion.PPOCRV6, ModelType.MEDIUM),
        "japanese": (LangRec.CH, OCRVersion.PPOCRV6, ModelType.MEDIUM),
    }[script]

    if script not in _rapid_engines:
        lang, version, size = spec
        _rapid_engines[script] = RapidOCR(params={
            "Rec.lang_type": lang, "Rec.ocr_version": version,
            "Rec.model_type": size, "Global.log_level": "error",
        })

    result = _rapid_engines[script](np.array(image.convert("RGB")))
    if not result or not result.txts:
        return Reading(script, "", 0.0)
    return Reading(script, " ".join(result.txts), float(np.mean(result.scores)))


READERS = {"vision": _read_vision, "rapidocr": _read_rapidocr}


def read(image, script: str, engines: list[str] | None = None) -> Reading:
    """OCR one page image, told which script it is."""
    return READERS[engine_for(script, engines)](image, script)


def detect_script(image, engines: list[str] | None = None) -> str:
    """Work out which script a page is written in.

    Costs one OCR pass per candidate — about four times a single read on
    Vision. Callers should do this once per document rather than once per page,
    and skip it entirely when the language is already known.
    """
    candidates = sorted(supported_scripts(engines))
    if not candidates:
        raise NoEngineFor("no OCR engine is installed")

    readings = [read(image, s, engines) for s in candidates]
    best = choose(readings)
    if best is None:
        raise NoEngineFor(
            "no OCR engine recovered any text from this page — it may be blank"
        )
    return best.script
