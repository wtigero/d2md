"""Character error rate, reported per script.

Six scripts is six verdicts, not one. Thai has no word boundaries and stacks
diacritics; Devanagari forms conjuncts; CJK has a glyph set in the thousands.
They fail in different ways, and an engine that wins on the average can be
unusable on one of them — so nothing here ever averages across scripts.

Two numbers per file:

    cer        edit distance over the text with whitespace collapsed
    cer_ns     the same with all whitespace removed

`cer_ns` is the one to compare across scripts. Thai, Japanese and Chinese are
written without spaces between words, so any engine that inserts them is
punished by `cer` for something that is not an error — while for English the
spaces are real and `cer` is the honest figure.
"""

from __future__ import annotations

import re
import unicodedata


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            )
        prev = cur
    return prev[-1]


def fold_digits(text: str) -> str:
    """Write every decimal digit in ASCII, whatever script it arrived in.

    EasyOCR renders `4471-9920-118` on a Hindi page as `४४७१-९९२०-११८`. That is
    the same number in Devanagari numerals, not a misreading — but scored
    against ASCII ground truth it looked like one, and it was the difference
    between calling Hindi unsolved and usable: 0.192 against 0.068 on the same
    page.

    Folding is the right call for what d2md is for. Text going into a search
    index or a model should carry one spelling of a number, and ASCII is the
    one every other engine here already emits. Any converter that keeps the
    native numerals should fold them the same way rather than be scored as
    wrong for a choice that is defensible.
    """
    for base in (0x0966, 0x0E50, 0x0660, 0x09E6, 0x0966, 0xFF10):
        if any(base <= ord(c) <= base + 9 for c in text):
            text = "".join(
                str(ord(c) - base) if base <= ord(c) <= base + 9 else c for c in text
            )
    return text


def normalise(text: str) -> str:
    """NFC, collapse whitespace, drop markdown scaffolding.

    Docling returns markdown, pypdfium2 returns raw text. Comparing them
    against the same ground truth means neither may be credited or charged for
    heading markers the other does not emit.
    """
    text = fold_digits(unicodedata.normalize("NFC", text))
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("*", "").replace("_", "")
    return re.sub(r"\s+", " ", text).strip()


def cer(pred: str, truth: str, ignore_space: bool = False) -> float:
    """Edit distance over `truth`. 0.0 is perfect; >1.0 is possible."""
    p, t = normalise(pred), normalise(truth)
    if ignore_space:
        p, t = re.sub(r"\s", "", p), re.sub(r"\s", "", t)
    if not t:
        return 0.0 if not p else 1.0
    return _levenshtein(p, t) / len(t)


def _substring_distance(needle: str, haystack: str) -> int:
    """Edit distance from `needle` to the closest substring of `haystack`.

    Standard edit distance with a free first row: starting anywhere in the
    haystack costs nothing, and so does stopping anywhere.
    """
    if not needle:
        return 0
    if not haystack:
        return len(needle)

    prev = [0] * (len(haystack) + 1)  # free start
    for i, cn in enumerate(needle, 1):
        cur = [i]
        for j, ch in enumerate(haystack, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cn != ch)))
        prev = cur
    return min(prev)  # free end


def cer_bag(pred: str, truth: str) -> float:
    """Character recovery, charging for neither reading order nor line breaks.

    Two artefacts made the plain edit distance misleading, in opposite
    directions. Concatenating the page charges a swapped heading at twice the
    line length — Docling reads a clean English scan perfectly and scores 0.269
    for putting the title second. Matching line against line instead fixes that
    but introduces the mirror problem: an engine that breaks its lines
    differently while emitting identical text scored 0.686 on a page it had
    read perfectly.

    So each truth line is aligned against the whole prediction, wherever it
    appears in it. This asks only "were these characters recovered", which is
    the question OCR is being judged on. It deliberately does not notice
    duplicated or invented text — `cer` does, and the two are reported
    together for that reason.
    """
    t_lines = [ln for ln in normalise_lines(truth) if ln]
    body = "".join(normalise_lines(pred))
    total = sum(len(ln) for ln in t_lines)
    if not total:
        return 0.0 if not body else 1.0
    return sum(_substring_distance(ln, body) for ln in t_lines) / total


def normalise_lines(text: str) -> list[str]:
    """Per-line normalisation, whitespace removed within each line.

    Spaces go because Thai, Japanese and Chinese are written without them and
    every engine disagrees about where to insert them.
    """
    text = fold_digits(unicodedata.normalize("NFC", text))
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("*", "").replace("_", "")
    return [re.sub(r"\s", "", ln) for ln in text.splitlines()]


def grade(value: float) -> str:
    """A word for a number, so the tables can be read at a glance."""
    if value < 0.02:
        return "excellent"
    if value < 0.10:
        return "usable"
    if value < 0.30:
        return "degraded"
    return "unusable"
