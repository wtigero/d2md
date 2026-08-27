from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from d2md import cli
from d2md.errors import ConversionError


@contextmanager
def passthrough(source, limits):
    yield source.path


def fake_success(seen):
    def run(path, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            markdown="converted text long enough",
            backend="pypdfium2",
        )

    return run


def test_default_forwards_no_heavy_mode(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    seen = []
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", fake_success(seen))

    assert cli.main([str(source), "-o", str(tmp_path / "out")]) == 0
    assert seen == [
        {
            "fast": None,
            "lang": None,
            "limits": cli.DEFAULT_LIMITS,
            "device": "auto",
            "ocr": False,
            "docling": False,
            "display_name": "report.pdf",
        }
    ]


def test_ocr_and_docling_flags_are_forwarded(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"fake")
    seen = []
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", fake_success(seen))
    monkeypatch.setattr(cli, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(cli, "ensure_docling_available", lambda: None)

    assert cli.main(
        [
            str(source),
            "--ocr",
            "--docling",
            "-o",
            str(tmp_path / "out"),
        ]
    ) == 0
    assert seen[0]["ocr"] is True
    assert seen[0]["docling"] is True


def test_missing_ocr_fails_before_collection(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "ensure_ocr_available",
        lambda: (_ for _ in ()).throw(
            ConversionError("install d2md[ocr]")
        ),
    )
    monkeypatch.setattr(
        cli, "collect", lambda *args, **kwargs: pytest.fail("collection used")
    )

    assert cli.main(["scan.pdf", "--ocr"]) == 2
    assert "d2md[ocr]" in capsys.readouterr().err


def test_missing_docling_fails_before_collection(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "ensure_docling_available",
        lambda: (_ for _ in ()).throw(
            ConversionError("install d2md[docling]")
        ),
    )
    monkeypatch.setattr(
        cli, "collect", lambda *args, **kwargs: pytest.fail("collection used")
    )

    assert cli.main(["report.pdf", "--docling"]) == 2
    assert "d2md[docling]" in capsys.readouterr().err


def test_lang_requires_ocr(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["scan.pdf", "--lang", "thai"])

    assert raised.value.code == 2
    assert "--lang requires --ocr" in capsys.readouterr().err


def test_lang_relationship_is_checked_before_script_name(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["scan.pdf", "--lang", "not-a-script"])

    assert raised.value.code == 2
    assert "--lang requires --ocr" in capsys.readouterr().err


def test_non_auto_device_requires_docling(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["scan.pdf", "--device", "cuda"])

    assert raised.value.code == 2
    assert "--device requires --docling" in capsys.readouterr().err


def test_deprecated_fast_is_hidden_and_warns(tmp_path, monkeypatch, capsys):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fake")
    seen = []
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", fake_success(seen))

    assert cli.main(
        [str(source), "--fast", "-o", str(tmp_path / "out")]
    ) == 0
    assert seen[0]["fast"] is True
    assert "--fast is deprecated" in capsys.readouterr().err


def test_engines_without_ocr_prints_repair_command(monkeypatch, capsys):
    monkeypatch.setattr("d2md.ocr.available_engines", lambda: [])

    assert cli.main(["--engines"]) == 1
    output = capsys.readouterr().err
    assert "no OCR engine installed" in output
    assert (
        'install it with: uv tool install --force "d2md[ocr]"'
    ) in output


def test_version_reports_the_distribution_version(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == "d2md 0.1.0\n"
