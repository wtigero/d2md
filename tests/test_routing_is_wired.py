"""Integration tests for explicit text, OCR, and Docling routing."""

import importlib

import pytest

from d2md.convert import ConversionError, convert
from d2md.ocr import ENGINE_SCRIPTS


convert_module = importlib.import_module("d2md.convert")


@pytest.fixture
def spy(monkeypatch):
    """Record Docling requests without loading a model."""
    calls = []

    def fake(
        backend,
        path,
        *,
        max_output_chars,
        timeout_seconds,
        validated_formats=frozenset(),
        script=None,
        force_ocr=False,
        device="auto",
        ocr_enabled=False,
    ):
        calls.append(
            {
                "backend": backend,
                "path": path,
                "max_output_chars": max_output_chars,
                "timeout_seconds": timeout_seconds,
                "validated_formats": validated_formats,
                "script": script,
                "force_ocr": force_ocr,
                "device": device,
                "ocr_enabled": ocr_enabled,
            }
        )
        return "x" * 500

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake)
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: frozenset()
    )
    monkeypatch.setattr(
        convert_module, "ensure_docling_available", lambda: None
    )
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    return calls


@pytest.fixture
def as_thai(monkeypatch):
    monkeypatch.setattr(
        convert_module, "script_of", lambda path, lang=None, **_: lang or "thai"
    )


def test_scan_without_ocr_names_install_and_flag(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: frozenset()
    )
    monkeypatch.setattr(
        convert_module, "_via_pypdfium2", lambda *args, **kwargs: [""]
    )

    with pytest.raises(ConversionError, match=r"d2md\[ocr\].*--ocr"):
        convert(source)


def test_installed_extras_do_not_change_default_pdf(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(convert_module, "_validate_input", lambda *args: None)
    monkeypatch.setattr(
        convert_module,
        "_via_pypdfium2",
        lambda *args, **kwargs: ["x" * 40],
    )
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda *args, **kwargs: pytest.fail("Docling used"),
    )

    assert convert(source).backend == "pypdfium2"


def test_healthy_pdf_with_ocr_still_skips_ocr(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(convert_module, "_validate_input", lambda *args: None)
    monkeypatch.setattr(
        convert_module,
        "_via_pypdfium2",
        lambda *args, **kwargs: ["x" * 40],
    )
    monkeypatch.setattr(
        convert_module,
        "convert_with_ocr",
        lambda *args, **kwargs: pytest.fail("OCR used"),
    )

    assert convert(source, ocr=True).backend == "pypdfium2"


def test_unknown_format_uses_markitdown_without_docling(tmp_path, monkeypatch):
    source = tmp_path / "data.unknown"
    source.write_bytes(b"fake")
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: frozenset()
    )
    monkeypatch.setattr(
        convert_module, "_run_isolated_backend", lambda *args, **kwargs: "x" * 40
    )
    monkeypatch.setattr(
        convert_module,
        "_via_docling",
        lambda *args, **kwargs: pytest.fail("child-local helper used in parent"),
    )

    assert convert(source).backend == "markitdown"


def test_scan_with_ocr_uses_direct_engine(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: frozenset()
    )
    monkeypatch.setattr(
        convert_module, "_via_pypdfium2", lambda *args, **kwargs: [""]
    )
    monkeypatch.setattr(convert_module, "script_of", lambda *args, **kwargs: "latin")
    monkeypatch.setattr(
        convert_module,
        "convert_with_ocr",
        lambda path, script, limits: ("recognized text " * 4, "rapidocr"),
    )

    assert convert(source, ocr=True).backend == "rapidocr"


def test_pdf_markers_do_not_make_short_ocr_output_usable(
    tmp_path, monkeypatch
):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(convert_module, "_validate_input", lambda *args: None)
    monkeypatch.setattr(
        convert_module, "_via_pypdfium2", lambda *args, **kwargs: [""]
    )
    monkeypatch.setattr(
        convert_module, "script_of", lambda *args, **kwargs: "latin"
    )
    monkeypatch.setattr(
        convert_module,
        "convert_with_ocr",
        lambda path, script, limits: (
            "<!-- Page number: 1 -->\n\nx\n",
            "rapidocr",
        ),
    )

    with pytest.raises(ConversionError, match=r"no usable text \(1 chars\)"):
        convert(source, ocr=True)


@pytest.mark.parametrize("ocr,script", [(False, None), (True, "thai")])
def test_docling_receives_explicit_ocr_state(
    tmp_path, monkeypatch, ocr, script
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    calls = []
    monkeypatch.setattr(
        convert_module, "ensure_docling_available", lambda: None
    )
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: frozenset()
    )
    monkeypatch.setattr(convert_module, "script_of", lambda *args, **kwargs: "thai")
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda backend,
        path,
        *,
        max_output_chars,
        timeout_seconds,
        validated_formats=frozenset(),
        script=None,
        force_ocr=False,
        device="auto",
        ocr_enabled=False: calls.append(
            (
                backend,
                max_output_chars,
                timeout_seconds,
                validated_formats,
                script,
                force_ocr,
                device,
                ocr_enabled,
            )
        )
        or "x" * 40,
    )

    expected_backend = "docling+ocr" if ocr else "docling"
    assert convert(
        source, ocr=ocr, docling=True, device="cpu"
    ).backend == expected_backend
    assert calls == [
        (
            "docling",
            20_000_000,
            1_800,
            frozenset(),
            script,
            False,
            "cpu",
            ocr,
        )
    ]


def test_damaged_thai_forces_docling_ocr_only_when_requested(
    tmp_path, monkeypatch, spy, as_thai
):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"fake")
    damaged = "ประกนอบตเหตสวนบคคล ระบบสงสนคาถงบานทวประเทศไทย " * 60
    monkeypatch.setattr(
        convert_module, "_via_pypdfium2", lambda *args, **kwargs: [damaged]
    )

    result = convert(source, ocr=True, docling=True, device="cpu")

    assert result.backend == "docling+ocr"
    assert len(spy) == 1
    call = spy[0]
    assert {key: value for key, value in call.items() if key != "path"} == {
        "backend": "docling",
        "max_output_chars": 20_000_000,
        "timeout_seconds": 1_800,
        "validated_formats": frozenset(),
        "script": "thai",
        "force_ocr": True,
        "device": "cpu",
        "ocr_enabled": True,
    }
    assert call["path"] != source
    assert call["path"].name.startswith("d2md-input-")
    assert call["path"].suffix == source.suffix


def test_docling_without_ocr_never_detects_a_script(tmp_path, monkeypatch, spy):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    monkeypatch.setattr(
        convert_module,
        "script_of",
        lambda *args, **kwargs: pytest.fail("script detection used"),
    )

    assert convert(source, docling=True).backend == "docling"
    assert spy[0]["script"] is None
    assert spy[0]["ocr_enabled"] is False


def test_device_requires_docling(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("plain text long enough for conversion")

    with pytest.raises(
        ConversionError, match="device selection requires docling=True"
    ):
        convert(source, device="cuda")


def test_lang_requires_ocr(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("plain text long enough for conversion")

    with pytest.raises(ConversionError, match="lang requires ocr=True"):
        convert(source, lang="thai")


def test_invalid_device_fails_before_plain_text_routing(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("plain text long enough to pass output validation")

    with pytest.raises(ConversionError, match="unknown device 'gpu'"):
        convert(source, device="gpu")


def test_each_script_maps_to_a_code_in_its_own_script():
    expected = {
        "thai": "th",
        "japanese": "ja",
        "korean": "ko",
        "cyrillic": "ru",
        "arabic": "ar",
        "latin": "en",
        "chinese": "zh",
    }
    for script, prefix in expected.items():
        code = ENGINE_SCRIPTS["vision"][script]
        assert code is not None, script
        assert code.startswith(prefix), f"vision {script} -> {code}"


def test_devanagari_stays_unsupported_everywhere():
    assert all(
        table.get("devanagari") is None for table in ENGINE_SCRIPTS.values()
    )
