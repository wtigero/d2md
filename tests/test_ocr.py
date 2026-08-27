"""Script detection, as pure string scoring — no images, no models.

The cases that matter here are the ones that were once wrong. A monolingual
test set passed the original detector cleanly; it was a Thai quotation quoting
English product names that exposed it, so those documents are the fixtures.
"""

import platform
import sys
import textwrap

import pytest

import d2md.ocr as ocr_module
from d2md.ocr import (
    ENGINE_SCRIPTS,
    NoEngineFor,
    Reading,
    available_engines,
    choose,
    engine_for,
    script_fit,
    significant,
    supported_scripts,
)

THAI = "สรุปกรมธรรม์ประกันอุบัติเหตุส่วนบุคคล เบี้ยประกันภัยรายปีจำนวนหนึ่งหมื่นสองพันบาท"
# 38% Latin by character count, and entirely Thai in meaning.
THAI_MIXED = (
    "ใบเสนอราคา / Quotation ผลิตภัณฑ์ Cloud Storage Premium จำนวน 3 licence "
    "ราคารวม 45,000 บาท (VAT included) ติดต่อ Sales Department ต่อ 220"
)
ENGLISH = "Personal Accident Insurance Policy Summary. The annual premium is payable."
JAPANESE = "個人傷害保険契約の概要です。年間保険料は四回に分けて支払います。"
CHINESE = "个人意外伤害保险合同摘要。年度保费可分四期支付。"
KOREAN = "개인 상해 보험 계약 요약입니다. 연간 보험료는 네 번에 나누어 납부합니다."


@pytest.fixture
def fake_rapidocr(tmp_path, monkeypatch):
    missing = object()
    original_rapidocr = sys.modules.pop("rapidocr", missing)
    original_onnxruntime = sys.modules.pop("onnxruntime", missing)
    (tmp_path / "rapidocr.py").write_text(
        textwrap.dedent(
            """
            import os
            import onnxruntime

            if os.environ.get("ORT_DISABLE_TELEMETRY") != "1":
                raise RuntimeError("ONNX telemetry opt-out was applied too late")
            if not onnxruntime.disabled:
                raise RuntimeError("ONNX telemetry API opt-out was applied too late")

            class LangRec:
                EN = "en"
                CH = "ch"

            class ModelType:
                MOBILE = "mobile"
                MEDIUM = "medium"

            class OCRVersion:
                PPOCRV5 = "v5"
                PPOCRV6 = "v6"

            class _Result:
                txts = ["portable OCR result"]
                scores = [1.0]

            class RapidOCR:
                def __init__(self, params):
                    self.params = params

                def __call__(self, image):
                    return _Result()
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "onnxruntime.py").write_text(
        textwrap.dedent(
            """
            calls = []
            disabled = False

            def disable_telemetry_events():
                global disabled
                disabled = True
                calls.append("disabled")
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield
    sys.modules.pop("rapidocr", None)
    sys.modules.pop("onnxruntime", None)
    if original_rapidocr is not missing:
        sys.modules["rapidocr"] = original_rapidocr
    if original_onnxruntime is not missing:
        sys.modules["onnxruntime"] = original_onnxruntime


def test_available_engines_opts_out_before_importing_rapidocr(
    fake_rapidocr, monkeypatch
):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    assert available_engines() == ["rapidocr"]
    import onnxruntime

    assert onnxruntime.calls == ["disabled"]


def test_direct_rapidocr_opts_out_before_importing_runtime(
    fake_rapidocr, monkeypatch
):
    class Image:
        @staticmethod
        def convert(_mode):
            return [[[0, 0, 0]]]

    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    ocr_module._rapid_engines.clear()
    try:
        reading = ocr_module._read_rapidocr(Image(), "latin")
    finally:
        ocr_module._rapid_engines.clear()

    assert reading.text == "portable OCR result"
    assert reading.confidence == 1.0
    import onnxruntime

    assert onnxruntime.calls == ["disabled"]


def test_significant_drops_shared_characters():
    """Every document carries the same policy number; it identifies nothing."""
    assert significant("abc 4471-9920-118!") == "abc"


@pytest.mark.parametrize(
    "text,script",
    [
        (ENGLISH, "latin"),
        (THAI, "thai"),
        (JAPANESE, "japanese"),
        (CHINESE, "chinese"),
        (KOREAN, "korean"),
        ("Полис номер выдан марта года", "cyrillic"),
        ("ملخص وثيقة التأمين ضد الحوادث", "arabic"),
    ],
)
def test_script_fit_recognises_its_own_script(text, script):
    assert script_fit(text, script) > 0.9


def test_latin_does_not_count_against_an_asian_script():
    """A Thai invoice quoting English product names is still Thai.

    Scoring Latin against Thai is what made the detector pick English on this
    exact document, taking it from CER 0.000 to 0.663.
    """
    assert script_fit(THAI_MIXED, "thai") > 0.95


def test_a_latin_page_scores_nothing_for_other_scripts():
    """No non-Latin evidence means no claim, which is what lets English win."""
    for script in ("thai", "japanese", "korean", "cyrillic", "arabic"):
        assert script_fit(ENGLISH, script) == 0.0


def test_kana_separate_japanese_from_chinese():
    """Han alone cannot: Japanese read as Chinese scores 0.627 in practice."""
    assert script_fit(JAPANESE, "japanese") > script_fit(JAPANESE, "chinese")
    assert script_fit(CHINESE, "chinese") > script_fit(CHINESE, "japanese")


# --- choosing between readings ---------------------------------------------


def test_the_fuller_reading_wins():
    """An engine asked for the wrong script returns the fragment it could read.

    Apple Vision on this page, asked for English, returns the Latin words at
    high confidence; asked for Thai it returns everything. Confidence alone
    picks the wrong one, which is the bug this guards.
    """
    fragment = Reading("latin", "Cloud Storage Premium Sales Department", 0.83)
    full = Reading("thai", THAI_MIXED, 1.00)
    assert choose([fragment, full]) is full


def test_a_short_confident_answer_loses_to_a_long_one():
    assert choose([
        Reading("latin", "02-116-4400", 1.0),
        Reading("thai", THAI, 0.9),
    ]).script == "thai"


def test_choose_ignores_empty_readings():
    assert choose([Reading("thai", "", 1.0), Reading("latin", ENGLISH, 0.5)]).script == (
        "latin"
    )


def test_choose_returns_none_when_nothing_was_read():
    assert choose([Reading("thai", "", 0.0), Reading("latin", "   ", 0.0)]) is None


# --- engine selection ------------------------------------------------------


def test_vision_is_preferred_where_both_exist():
    assert engine_for("chinese", ["vision", "rapidocr"]) == "vision"


def test_falls_through_to_rapidocr_when_vision_is_absent():
    assert engine_for("chinese", ["rapidocr"]) == "rapidocr"


def test_rapidocr_is_not_offered_for_scripts_it_reads_badly():
    """It ships a Korean model and scores 0.730 with it. Shipping is not support."""
    for script in ("thai", "korean", "cyrillic", "arabic"):
        assert ENGINE_SCRIPTS["rapidocr"][script] is None
        with pytest.raises(NoEngineFor):
            engine_for(script, ["rapidocr"])


def test_an_unreadable_script_raises_rather_than_guessing():
    """The failure being replaced was silent: Korean OCR'd as English returned
    nothing and exited zero."""
    with pytest.raises(NoEngineFor, match="devanagari"):
        engine_for("devanagari", ["vision", "rapidocr"])


def test_no_engines_at_all_raises():
    with pytest.raises(NoEngineFor):
        engine_for("latin", [])


def test_supported_scripts_is_the_union():
    assert supported_scripts(["vision"]) == {
        "latin", "thai", "japanese", "chinese", "korean", "cyrillic", "arabic"
    }
    assert supported_scripts(["rapidocr"]) == {"latin", "chinese", "japanese"}
