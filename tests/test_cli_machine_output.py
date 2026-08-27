import io
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from d2md import cli
from d2md import capabilities
from d2md.errors import ConversionError


MARKDOWN = "# Quarterly report\n\nConverted text long enough.\n"


@contextmanager
def passthrough(source, limits):
    yield source.path


def successful_conversion(path, **kwargs):
    return SimpleNamespace(markdown=MARKDOWN, backend="pypdfium2")


def noisy_conversion(path, **kwargs):
    print("backend initialization noise")
    return successful_conversion(path, **kwargs)


def test_stdout_emits_only_markdown_and_does_not_create_output(
    tmp_path, monkeypatch, capsys
):
    """Removing stdout mode would mix progress into a pipe and write a file."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", successful_conversion)

    assert cli.main([str(source), "--stdout"]) == 0

    captured = capsys.readouterr()
    assert captured.out == MARKDOWN
    assert captured.err == ""
    assert not (tmp_path / "md-out").exists()


def test_stdout_redirects_backend_noise_away_from_markdown(
    tmp_path, monkeypatch, capsys
):
    """A backend print must not corrupt Markdown consumed by another process."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", noisy_conversion)

    assert cli.main([str(source), "--stdout"]) == 0

    captured = capsys.readouterr()
    assert captured.out == MARKDOWN
    assert "backend initialization noise" in captured.err


def test_stdout_preserves_markdown_bytes_when_not_a_terminal(
    tmp_path, monkeypatch, capsys
):
    """Adding a newline changes Markdown consumed through a pipe."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(
        cli,
        "convert",
        lambda path, **kwargs: SimpleNamespace(
            markdown="# report", backend="pypdfium2"
        ),
    )

    assert cli.main([str(source), "--stdout"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "# report"
    assert captured.err == ""


def test_stdout_refuses_terminal_control_sequences_on_a_tty(tmp_path, monkeypatch):
    """Writing OSC markup to a terminal would let a document control that terminal."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    terminal = io.StringIO()
    terminal.isatty = lambda: True
    errors = io.StringIO()
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(
        cli,
        "convert",
        lambda path, **kwargs: SimpleNamespace(
            markdown="[report]\x1b]8;;https://attacker.invalid\x1b\\",
            backend="pypdfium2",
        ),
    )
    monkeypatch.setattr(cli.sys, "stdout", terminal)
    monkeypatch.setattr(cli.sys, "stderr", errors)

    assert cli.main([str(source), "--stdout"]) == 1

    assert terminal.getvalue() == ""
    assert "terminal control" in errors.getvalue()


def test_stdout_refuses_ambiguous_multiple_inputs(tmp_path, monkeypatch, capsys):
    """Accepting multiple inputs would produce an unlabeled Markdown stream."""
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first source content long enough", encoding="utf-8")
    second.write_text("second source content long enough", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "convert",
        lambda *args, **kwargs: pytest.fail("conversion must not start"),
    )

    assert cli.main([str(first), str(second), "--stdout"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--stdout requires exactly one input" in captured.err


@pytest.mark.parametrize(
    "extra",
    [["--force"], ["-o", "another-directory"], ["-o", "md-out"]],
)
def test_stdout_rejects_file_output_options(extra, capsys):
    """Silently ignoring file-output options would make automation ambiguous."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["report.txt", "--stdout", *extra])

    assert raised.value.code == 2
    assert "--stdout does not write output files" in capsys.readouterr().err


def test_json_success_is_one_machine_readable_document(
    tmp_path, monkeypatch, capsys
):
    """Restoring human progress in JSON mode would corrupt agent parsing."""
    source = tmp_path / "report.txt"
    outdir = tmp_path / "converted"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", successful_conversion)

    assert cli.main([str(source), "--json", "-o", str(outdir)]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["options"] == {
        "device": "auto",
        "docling": False,
        "language": None,
        "ocr": False,
    }
    assert payload["summary"] == {"converted": 1, "failed": 0, "skipped": 0}
    assert payload["errors"] == []
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["source"] == str(source)
    assert result["output"] == str(outdir / "report.md")
    assert result["status"] == "converted"
    assert result["backend"] == "pypdfium2"
    assert result["characters"] == len(MARKDOWN)
    assert isinstance(result["seconds"], float)
    assert result["seconds"] >= 0
    assert (outdir / "report.md").read_text(encoding="utf-8") == MARKDOWN


def test_json_redirects_backend_noise_away_from_report(
    tmp_path, monkeypatch, capsys
):
    """A backend print must not make the JSON document unparsable."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", noisy_conversion)

    assert cli.main([str(source), "--json", "-o", str(tmp_path / "out")]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "backend initialization noise" in captured.err


def test_json_conversion_failure_stays_structured(tmp_path, monkeypatch, capsys):
    """Printing a backend failure outside JSON would break automation."""
    source = tmp_path / "broken.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)

    def fail_conversion(path, **kwargs):
        raise ConversionError("backend rejected the document")

    monkeypatch.setattr(cli, "convert", fail_conversion)

    assert cli.main([str(source), "--json", "-o", str(tmp_path / "out")]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["summary"] == {"converted": 0, "failed": 1, "skipped": 0}
    assert payload["results"] == []
    assert payload["errors"] == [
        {"source": str(source), "message": "backend rejected the document"}
    ]


def test_json_wire_escapes_terminal_control_characters(
    tmp_path, monkeypatch, capsys
):
    """A crafted filename must not inject bidi controls into terminal output."""
    source = tmp_path / "quarterly\u202ereport.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "_snapshot_input", passthrough)
    monkeypatch.setattr(cli, "convert", successful_conversion)

    assert cli.main([str(source), "--json", "-o", str(tmp_path / "out")]) == 0

    output = capsys.readouterr().out
    assert "\u202e" not in output
    assert "\\u202e" in output
    assert json.loads(output)["results"][0]["source"] == str(source)


def test_json_preflight_failure_stays_structured(monkeypatch, capsys):
    """An unavailable requested capability must be actionable JSON, not prose."""
    monkeypatch.setattr(
        cli,
        "ensure_ocr_available",
        lambda: (_ for _ in ()).throw(ConversionError("OCR is not installed")),
    )

    assert cli.main(["scan.pdf", "--ocr", "--json"]) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["summary"] == {"converted": 0, "failed": 1, "skipped": 0}
    assert payload["errors"] == [
        {"source": None, "message": "OCR is not installed"}
    ]


@pytest.mark.parametrize("engines", [[], ["rapidocr"]])
def test_capabilities_json_succeeds_with_or_without_optional_engines(
    engines, monkeypatch, capsys
):
    """Capability discovery must not fail when an optional profile is absent."""
    monkeypatch.setattr("d2md.ocr.available_engines", lambda: engines)
    monkeypatch.setattr(
        capabilities, "module_available", lambda name: name == "docling"
    )

    assert cli.main(["--capabilities", "--json"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["schema_version"] == 1
    assert payload["ocr"]["installed"] is bool(engines)
    assert payload["ocr"]["install_command"] == (
        'uv tool install --force "d2md[ocr]"'
    )
    assert payload["ocr"]["scripts"] == (
        ["chinese", "japanese", "latin"] if engines else []
    )
    assert payload["ocr"]["engines"] == (
        [
            {
                "name": "rapidocr",
                "scripts": ["chinese", "japanese", "latin"],
            }
        ]
        if engines
        else []
    )
    assert payload["docling"] == {
        "installed": True,
        "device_choices": ["auto", "cpu", "cuda", "mps", "xpu"],
        "install_command": (
            'uv tool install --force "d2md[docling]"'
        ),
    }
