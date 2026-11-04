import io
import json
from types import SimpleNamespace

import pytest

from d2md import cli
from d2md import capabilities
from d2md.errors import ConversionError


MARKDOWN = "# Quarterly report\n\nConverted text long enough.\n"


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
    monkeypatch.setattr(cli, "convert", successful_conversion)

    assert cli.main([str(source), "--stdout"]) == 0

    captured = capsys.readouterr()
    assert captured.out == MARKDOWN
    assert captured.err == ""
    assert not (tmp_path / "md-out").exists()


def test_stdout_preserves_pdf_page_markers(
    multi_page_text_pdf, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    assert cli.main([str(multi_page_text_pdf), "--stdout"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("<!-- Page number:") == 3
    assert "<!-- Page number: 1 -->" in captured.out
    assert "<!-- Page number: 2 -->" in captured.out
    assert "<!-- Page number: 3 -->" in captured.out
    assert not (tmp_path / "md-out").exists()


def test_stdout_redirects_backend_noise_away_from_markdown(
    tmp_path, monkeypatch, capsys
):
    """A backend print must not corrupt Markdown consumed by another process."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
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


def test_file_and_json_report_preserve_pdf_page_markers(
    multi_page_text_pdf, tmp_path, capsys
):
    outdir = tmp_path / "converted"

    assert cli.main(
        [str(multi_page_text_pdf), "--json", "-o", str(outdir)]
    ) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    markdown = (outdir / "multi-page.md").read_text(encoding="utf-8")
    assert captured.err == ""
    assert markdown.count("<!-- Page number:") == 3
    assert payload["results"][0]["characters"] == len(markdown)


def test_json_redirects_backend_noise_away_from_report(
    tmp_path, monkeypatch, capsys
):
    """A backend print must not make the JSON document unparsable."""
    source = tmp_path / "report.txt"
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.setattr(cli, "convert", noisy_conversion)

    assert cli.main([str(source), "--json", "-o", str(tmp_path / "out")]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "backend initialization noise" in captured.err


def test_json_conversion_failure_stays_structured(tmp_path, monkeypatch, capsys):
    """Printing a backend failure outside JSON would break automation."""
    source = tmp_path / "broken.txt"
    source.write_text("source content long enough", encoding="utf-8")

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


def test_same_output_name_fails_the_later_input_instead_of_replacing_it(
    tmp_path, capsys
):
    first = tmp_path / "a" / "same.txt"
    second = tmp_path / "b" / "SAME.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first document body", encoding="utf-8")
    second.write_text("second document body", encoding="utf-8")
    outdir = tmp_path / "out"

    for options in ([], ["--force"]):
        assert cli.main([str(first), str(second), "-o", str(outdir), *options]) == 1
        output = capsys.readouterr().out
        assert "1 failed" in output
        assert "already used by same.txt" in output
        assert (outdir / "same.md").read_text(encoding="utf-8") == (
            "first document body"
        )


@pytest.mark.parametrize(
    "first_name, second_name",
    [
        ("\u0390.txt", "\u03aa\u0301.txt"),  # case folding undoes NFC
        ("caf\u00e9.txt", "cafe\u0301.txt"),
        ("Stra\u00dfe.txt", "strasse.txt"),
        ("\u03b1\u0345\u0301.txt", "\u03b1\u0301\u0345.txt"),  # NFC must come first
    ],
)
def test_names_a_filesystem_treats_as_one_file_collide(
    tmp_path, capsys, first_name, second_name
):
    first = tmp_path / "a" / first_name
    second = tmp_path / "b" / second_name
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first document body", encoding="utf-8")
    second.write_text("second document body", encoding="utf-8")

    code = cli.main([str(first), str(second), "-o", str(tmp_path / "out"), "--force"])

    assert code == 1
    assert "already used by" in capsys.readouterr().out
    assert [item.read_text(encoding="utf-8") for item in (tmp_path / "out").iterdir()] == [
        "first document body"
    ]


def test_output_from_an_earlier_run_still_claims_its_name(tmp_path, capsys):
    first = tmp_path / "a" / "same.txt"
    second = tmp_path / "b" / "same.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first document body", encoding="utf-8")
    second.write_text("second document body", encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "same.md").write_text("earlier run", encoding="utf-8")

    assert cli.main([str(first), str(second), "-o", str(outdir)]) == 1
    output = capsys.readouterr().out
    assert "1 skipped" in output
    assert "1 failed" in output
    assert (outdir / "same.md").read_text(encoding="utf-8") == "earlier run"


def test_one_file_spelled_two_ways_is_not_a_collision(tmp_path, capsys):
    source = tmp_path / "d" / "a.txt"
    source.parent.mkdir()
    source.write_text("one document body", encoding="utf-8")
    other_spelling = source.parent / ".." / "d" / "a.txt"

    code = cli.main([str(source), str(other_spelling), "-o", str(tmp_path / "out")])

    assert code == 0
    assert "already used" not in capsys.readouterr().out


def test_one_file_listed_twice_converts_once_even_with_force(tmp_path, capsys):
    source = tmp_path / "d" / "a.txt"
    source.parent.mkdir()
    source.write_text("one document body", encoding="utf-8")
    other_spelling = source.parent / ".." / "d" / "a.txt"
    command = [str(source), str(other_spelling), "-o", str(tmp_path / "out"), "--force"]

    assert cli.main(command) == 0
    assert "1 converted · 1 skipped · 0 failed" in capsys.readouterr().out


def test_directory_inputs_are_converted_inside_their_collected_root(
    tmp_path, monkeypatch
):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.txt").write_text("one document body", encoding="utf-8")
    seen = []

    def fake_convert(_path, **kwargs):
        seen.append(kwargs["root"])
        return SimpleNamespace(markdown="converted text", backend="plain")

    monkeypatch.setattr(cli, "convert", fake_convert)

    assert cli.main([str(inbox), "-o", str(tmp_path / "out")]) == 0
    assert seen == [inbox]


def test_directory_inputs_keep_their_folders(tmp_path):
    inbox = tmp_path / "inbox"
    for folder in ("a", "b"):
        (inbox / folder).mkdir(parents=True)
        (inbox / folder / "same.txt").write_text(f"{folder} document body", encoding="utf-8")
    (inbox / "top.txt").write_text("top document body", encoding="utf-8")
    outdir = tmp_path / "out"

    assert cli.main([str(inbox), "-o", str(outdir)]) == 0
    assert (outdir / "a" / "same.md").read_text(encoding="utf-8") == "a document body"
    assert (outdir / "b" / "same.md").read_text(encoding="utf-8") == "b document body"
    assert (outdir / "top.md").exists()


def test_many_output_folders_fit_in_few_file_descriptors(tmp_path):
    resource = pytest.importorskip("resource")
    import os

    inbox = tmp_path / "inbox"
    for number in range(80):
        (inbox / f"d{number}").mkdir(parents=True)
        (inbox / f"d{number}" / "a.txt").write_text("document body", encoding="utf-8")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = len(os.listdir("/dev/fd"))
    resource.setrlimit(resource.RLIMIT_NOFILE, (in_use + 40, hard))
    try:
        assert cli.main([str(inbox), "-o", str(tmp_path / "out"), "--quiet"]) == 0
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert (tmp_path / "out" / "d79" / "a.md").exists()


def test_folder_scans_skip_macos_and_office_companion_files(tmp_path):
    inbox = tmp_path / "usb"
    inbox.mkdir()
    (inbox / "report.txt").write_text("a real document body", encoding="utf-8")
    (inbox / "._report.txt").write_bytes(b"\x00\x05\x16\x07Mac OS X")
    (inbox / "._scan.pdf").write_bytes(b"\x00\x05\x16\x07Mac OS X")
    (inbox / "~$report.docx").write_bytes(b"\x05Owner" + b"\x00" * 150)

    files, failures = cli.collect([str(inbox)])

    assert [item.path.name for item in files] == ["report.txt"]
    assert failures == []


def test_a_full_batch_stops_converting(tmp_path, monkeypatch, capsys):
    from dataclasses import replace

    from d2md.convert import convert as real_convert

    inbox = tmp_path / "batch"
    inbox.mkdir()
    for number in range(5):
        (inbox / f"f{number}.txt").write_text("x" * 40, encoding="utf-8")
    calls = []

    def counting_convert(path, **kwargs):
        calls.append(path)
        return real_convert(path, **kwargs)

    monkeypatch.setattr(cli, "convert", counting_convert)
    monkeypatch.setattr(cli, "DEFAULT_LIMITS", replace(cli.DEFAULT_LIMITS, max_output_chars=50))

    assert cli.main([str(inbox), "-o", str(tmp_path / "out")]) == 1
    assert len(calls) == 2
    assert "rerun to continue" in capsys.readouterr().out
