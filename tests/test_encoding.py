"""Encoding tests.

The CP949 case is the one that matters: it is a real misdetection observed with
charset_normalizer, and it fails silently, so it is exactly the kind of bug a
test has to pin down.
"""

import sys
from types import SimpleNamespace

import pytest

from d2md.encoding import decode, read_text, thai_ratio
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
