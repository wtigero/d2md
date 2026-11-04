"""Encoding tests.

The CP949 case is the one that matters: it is a real misdetection observed with
charset_normalizer, and it fails silently, so it is exactly the kind of bug a
test has to pin down.
"""

import sys
from types import SimpleNamespace

import pytest

from d2md.encoding import THAI_MIN_RATIO, decode, read_text, thai_ratio
from d2md.errors import ConversionError

THAI = "ทดสอบภาษาไทย น้ำจำกัด ระบบอ่านไฟล์"


def test_utf8_roundtrip():
    text, enc = decode(THAI.encode("utf-8"))
    assert text == THAI
    assert enc.startswith("utf-8")


def test_utf8_bom():
    text, _ = decode(THAI.encode("utf-8-sig"))
    assert text == THAI


def test_cp874_thai_is_not_guessed_as_korean():
    raw = "ทดสอบภาษาไทย cp874 น้ำจำกัด".encode("cp874")
    text, enc = decode(raw)
    assert text == "ทดสอบภาษาไทย cp874 น้ำจำกัด"
    assert enc == "cp874"


def test_short_cp874_still_decodes():
    # 27 bytes was enough to make charset_normalizer answer CP949.
    raw = "ทดสอบภาษาไทย cp874 น้ำจำกัด".encode("cp874")
    assert len(raw) < 40
    assert thai_ratio(decode(raw)[0]) > 0.5


def test_ascii_is_untouched():
    text, _ = decode(b"plain ascii, nothing special")
    assert text == "plain ascii, nothing special"


def test_non_thai_bytes_do_not_get_forced_into_cp874():
    raw = "Привет, как дела".encode("utf-8")
    text, enc = decode(raw)
    assert text == "Привет, как дела"
    assert enc.startswith("utf-8")


def test_thai_ratio_bounds():
    assert thai_ratio("") == 0.0
    assert thai_ratio("abc") == 0.0
    assert thai_ratio("ไทย") == 1.0


def test_read_text_rejects_character_limit_before_returning_output(tmp_path):
    source = tmp_path / "oversized.txt"
    source.write_text("ภาษาไทย", encoding="utf-8")

    with pytest.raises(ConversionError, match="output limit exceeded"):
        read_text(source, max_bytes=100, max_chars=4)


def test_encoding_detection_uses_a_bounded_sample(monkeypatch):
    inspected_sizes = []

    class Matches:
        @staticmethod
        def best():
            return SimpleNamespace(encoding="latin-1")

    def from_bytes(payload):
        inspected_sizes.append(len(payload))
        return Matches()

    monkeypatch.setitem(
        sys.modules,
        "charset_normalizer",
        SimpleNamespace(from_bytes=from_bytes),
    )
    raw = b"\x81" * 100_000

    text, encoding = decode(raw, max_chars=len(raw))

    assert text == "\x81" * len(raw)
    assert encoding == "latin-1"
    assert inspected_sizes == [64 * 1024]


def test_encoding_detection_samples_beyond_a_long_ascii_preamble():
    legacy_text = (
        "Příliš žluťoučký kůň úpěl ďábelské ódy. "
        "Zażółć gęślą jaźń.\n"
    ) * 1_000
    raw = b"A" * 70_000 + legacy_text.encode("cp1250")

    text, encoding = decode(raw, max_chars=len(raw))

    assert text == "A" * 70_000 + legacy_text
    assert encoding.lower().replace("-", "") in {"cp1250", "windows1250"}


def test_cp874_english_with_one_thai_word_keeps_the_thai():
    raw = ("Invoice 12345, total payable in baht. " * 3 + "ราคารวม").encode("cp874")

    assert decode(raw) == (raw.decode("cp874"), "cp874")


def test_latin_accents_are_not_read_as_thai():
    text, encoding = decode("Caf\u00e9 r\u00e9sum\u00e9 for the meeting.".encode("cp1252"))

    assert encoding != "cp874"
    assert not any("\u0e00" <= char <= "\u0e7f" for char in text)


@pytest.mark.parametrize(
    "text",
    [
        "Die Größe der Straße ist gemäß Plan.",
        "Hun så på bøkene og sjøen før middag.",
        "Smørrebrød på bordet, æblet er rødt.",
        "Copyright © 2024 Acme Ltd. Price: £100 per unit, see § 3. "
        "Store at -20°C ±0.5 mm. ÉCOLE ÎLE.",
    ],
)
def test_latin_vowels_that_decode_to_thai_vowels_are_not_thai(text):
    decoded, encoding = decode(text.encode("cp1252"))

    assert encoding != "cp874"
    assert not any("฀" <= char <= "๿" for char in decoded)


def test_short_thai_words_beside_english_stay_thai():
    english = "Please send the signed copy back to the office before Friday. " * 3
    text = english + "Use ไฟล์PDF and Excelในการ work ได้ ไม่ พ.ศ. ๒๕๖๗ เร็ว ๆ"

    assert thai_ratio(text) < THAI_MIN_RATIO
    assert decode(text.encode("cp874")) == (text, "cp874")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            b'<meta charset="iso-8859-1"><p>He said \x93hi\x94 \x96 it\x92s</p>',
            "He said “hi” – it’s",
        ),
        (
            b'\xef\xbb\xbf<meta charset="iso-8859-1"><p>' + "Größe".encode() + b"</p>",
            "Größe",
        ),
        (b'<meta charset="utf-16"><p>Hello!</p>', "Hello!"),
        (
            b'<meta charset="utf-8"><p>' + "สวัสดีครับ".encode() + b" caf\xe9</p>",
            "สวัสดีครับ caf�",
        ),
        (b'<meta charset="undefined"><p>Hello!</p>', "Hello!"),
    ],
)
def test_html_charset_labels_follow_browsers(raw, expected):
    from d2md.encoding import decode_html

    assert expected in decode_html(raw)


@pytest.mark.parametrize(
    "meta",
    [
        '<meta http-equiv="Content-Type" content="text/html; charset=windows-874">',
        '<meta charset="tis-620">',
        "",
    ],
)
def test_thai_html_decodes_by_its_declared_or_detected_charset(meta):
    from d2md.encoding import decode_html

    thai = "สวัสดีครับ นี่คือเอกสารทดสอบภาษาไทย ราคาสินค้ารวมภาษีแล้ว"
    raw = f"<html><head>{meta}</head><body><p>{thai}</p></body></html>".encode("cp874")

    assert thai in decode_html(raw)


def test_declared_html_charset_wins_over_guessing():
    from d2md.encoding import decode_html

    # Every byte of this Russian is also valid CP874, so guessing reads Thai.
    raw = '<meta charset="windows-1251"><p>Привет мир</p>'.encode("cp1251")

    assert decode(raw)[0] != raw.decode("cp1251")
    assert "Привет мир" in decode_html(raw)
