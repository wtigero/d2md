"""An explicit OCR language must be readable before the first input starts."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from d2md import cli
from d2md import ocr


@contextmanager
def _passthrough_snapshot(source, limits):
    yield source.path


@pytest.fixture
def engines(monkeypatch):
    """Pin which OCR engines this machine reports, leaving the tables real."""

    monkeypatch.setattr(cli, "ensure_ocr_available", lambda: None)

    def install(*names):
        monkeypatch.setattr(ocr, "available_engines", lambda: list(names))

    return install


@pytest.fixture
def pdf(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    return source


def test_lang_this_machine_cannot_read_is_refused_before_any_file(
    capsys, engines, pdf, tmp_path
):
    engines("rapidocr")  # the portable engine has no Thai model

    exit_code = cli.main(
        [str(pdf), "--ocr", "--lang", "thai", "-o", str(tmp_path / "out")]
    )

    assert exit_code == 2
    message = capsys.readouterr().err
    assert "thai" in message
    assert "chinese, japanese, latin" in message


def test_known_but_unreadable_script_is_not_reported_as_unknown(
    capsys, engines, pdf, tmp_path
):
    """Devanagari is in BLOCKS but no engine has a model for it."""
    engines("vision", "rapidocr")

    exit_code = cli.main(
        [str(pdf), "--ocr", "--lang", "devanagari", "-o", str(tmp_path / "out")]
    )

    assert exit_code == 2
    message = capsys.readouterr().err
    assert "devanagari" in message
    assert "unknown script" not in message


def test_unrecognised_script_name_still_reports_unknown(capsys, pdf, tmp_path):
    exit_code = cli.main(
        [str(pdf), "--ocr", "--lang", "klingon", "-o", str(tmp_path / "out")]
    )

    assert exit_code == 2
    assert "unknown script" in capsys.readouterr().err


def test_machine_without_any_ocr_engine_says_so(capsys, engines, pdf, tmp_path):
    engines()

    exit_code = cli.main(
        [str(pdf), "--ocr", "--lang", "latin", "-o", str(tmp_path / "out")]
    )

    assert exit_code == 2
    assert "no OCR engine available" in capsys.readouterr().err


def test_readable_script_is_forwarded_to_the_converter(
    engines, monkeypatch, pdf, tmp_path
):
    engines("rapidocr")
    seen = []

    def fake_convert(path, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(markdown="converted output long enough", backend="docling")

    monkeypatch.setattr(cli, "convert", fake_convert)
    monkeypatch.setattr(cli, "_snapshot_input", _passthrough_snapshot)

    exit_code = cli.main(
        [str(pdf), "--ocr", "--lang", "latin", "-o", str(tmp_path / "out")]
    )

    assert exit_code == 0
    assert seen[0]["lang"] == "latin"
    assert seen[0]["ocr"] is True
