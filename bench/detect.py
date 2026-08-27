"""Pick which language an OCR engine should have been asked for.

Every engine here needs to be told the script in advance, and every one of them
returns confident nonsense when told the wrong one. Apple Vision asked for
English on a Thai page returns the eleven ASCII characters and no error at all.
So something has to choose, and the choice cannot come from the engine.

The approach is to run the engine once per candidate language and keep the
answer that looks most like the language it claims to be. Confidence alone is
not enough — on a degraded scan Vision reports high confidence for Japanese on
a Chinese page — so confidence is multiplied by how much of the returned text
actually falls in that language's script.

That second term is what separates Japanese from Chinese: they share Han
characters, so Han alone decides nothing, but kana appear only in Japanese.
"""

from __future__ import annotations

import unicodedata

# Ranges that identify a script. Han is deliberately shared between ja and zh —
# it is the kana that break the tie, so kana are weighted separately below.
BLOCKS = {
    "th": [(0x0E01, 0x0E5B)],
    "hi": [(0x0900, 0x097F)],
    "ko": [(0x1100, 0x11FF), (0xAC00, 0xD7AF), (0x3130, 0x318F)],
    "ja": [(0x3040, 0x30FF), (0x4E00, 0x9FFF)],
    "zh": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
    "en": [(0x0041, 0x005A), (0x0061, 0x007A)],
    "ru": [(0x0400, 0x04FF)],
    "ar": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    # Vietnamese, German and Traditional Chinese share their block with a
    # language already listed — Latin for the first two, Han for the third.
    # Unicode cannot separate them, so these entries are deliberately identical
    # to their neighbours and the detector is expected to fail between them.
    # That failure is the measurement, not an oversight.
    "vi": [(0x0041, 0x005A), (0x0061, 0x007A)],
    "de": [(0x0041, 0x005A), (0x0061, 0x007A)],
    "zt": [(0x4E00, 0x9FFF), (0x3400, 0x4DBF)],
}

KANA = [(0x3040, 0x309F), (0x30A0, 0x30FF)]


def _in(ch: str, ranges) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in ranges)


def _significant(text: str) -> str:
    """Drop digits, punctuation and whitespace.

    Every document in the corpus carries the same policy number and phone
    number. Those characters are identical in all six languages and would
    otherwise dilute the signal towards whichever language is longest.
    """
    return "".join(
        c for c in text
        if not c.isspace()
        and not c.isdigit()
        and unicodedata.category(c)[0] not in {"P", "S"}
    )


def script_fit(text: str, script: str) -> float:
    """How much of `text` is written in `script`, from 0.0 to 1.0.

    Latin is excluded from the denominator for every non-Latin script, because
    a Thai quotation quoting `Cloud Storage Premium` and `Net 30` is still a
    Thai document. Counting those characters against Thai is what made the
    detector pick English on a page that was 38% English by character count and
    entirely Thai in meaning — the Thai reading scored 0.62 against English's
    0.97, and lost, on a page it had read perfectly.

    So the question asked here is not "how much of this page is Thai" but "of
    the characters that could distinguish one script from another, how many are
    Thai".
    """
    body = _significant(text)
    if not body:
        return 0.0

    latin = sum(_in(c, BLOCKS["en"]) for c in body)
    if script == "en":
        return latin / len(body)

    distinguishing = len(body) - latin
    if not distinguishing:
        # Nothing but Latin on the page: no evidence for this script at all,
        # which is what lets a genuinely English document beat every other
        # candidate rather than tying with all of them.
        return 0.0

    hit = sum(_in(c, BLOCKS[script]) for c in body) / distinguishing

    # Han is shared. A Japanese page almost always carries kana; a Chinese page
    # never does. Without this, zh and ja score identically on Han-only text
    # and the tie is broken by nothing at all.
    if script == "ja" and not any(_in(c, KANA) for c in body):
        hit *= 0.5
    if script == "zh" and any(_in(c, KANA) for c in body):
        hit *= 0.3

    return min(hit, 1.0)


def score_of(text: str, script: str, conf: float, longest: int = 0) -> float:
    """Confidence, weighted by script fit and by how much text came back.

    An engine asked for the wrong language does not fail loudly — it returns
    the fragment it could read, confidently. On a Thai page asked for English,
    Apple Vision returns the Latin words and nothing else. So the answer that
    recovered the most text is the one that was asked the right question, and
    length is measured against the best candidate rather than an absolute
    threshold: pages differ in length, candidates on one page do not.
    """
    body = len(_significant(text))
    coverage = body / longest if longest else min(body / 40, 1.0)
    return conf * script_fit(text, script) * coverage


def choose(candidates: dict[str, tuple[str, float]]) -> tuple[str, str]:
    """Pick a language from {script: (text, mean_confidence)}.

    Returns (script, text). An engine that is sure of the wrong script loses to
    one that is less sure of the right one.
    """
    script, text, _ = choose_scored(candidates)
    return script, text


def choose_scored(
    candidates: dict[str, tuple[str, float]],
) -> tuple[str, str, float]:
    """As `choose`, but also returns the winning score.

    The score is what lets two engines be compared against each other rather
    than only their own candidates — which is how the ensemble picks between
    Apple Vision and RapidOCR on the same page.
    """
    longest = max(
        (len(_significant(t) or "") for t, _ in candidates.values()), default=0
    )
    best: tuple[str, str, float] = ("", "", -1.0)
    for script, (text, conf) in candidates.items():
        s = score_of(text, script, conf, longest)
        if s > best[2]:
            best = (script, text, s)
    return best
