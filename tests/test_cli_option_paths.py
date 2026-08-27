"""Regression tests for option-shaped filesystem operands."""

from contextlib import contextmanager
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from d2md import cli


AMBIGUOUS_NAMES = (
    "--unsafe-unlimited",
    "--ocr",
    "--docling",
    "--device=cpu",
    "--outdir=~",
    "--outdir=..",
    "--force",
    "-o",
    "-f",
)

MARKDOWN = "# isolated operand\n\nConverted text long enough.\n"


def _exit_code(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return int(exc.code)


def _forbidden_sink(*args, **kwargs):
    pytest.fail("option ambiguity reached a privileged or write-capable sink")


def _guard_privileged_sinks(monkeypatch) -> None:
    monkeypatch.setattr(cli, "ensure_ocr_available", _forbidden_sink)
    monkeypatch.setattr(cli, "ensure_docling_available", _forbidden_sink)
    monkeypatch.setattr(cli, "_open_output_directory", _forbidden_sink)
    monkeypatch.setattr(cli, "_write_output", _forbidden_sink)
    monkeypatch.setattr(cli, "convert", _forbidden_sink)


def _assert_safe_operand_guidance(diagnostic: str) -> None:
    lowered = diagnostic.lower()
    assert "options before '--'" in lowered
    assert "paths after it" in lowered
    assert "prefix the path with './'" in lowered


@contextmanager
def _passthrough_snapshot(source, limits):
    yield source.path


def test_privileged_cli_options_cannot_be_abbreviated(capsys):
    """``--uns`` must not silently enable the unlimited resource profile."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["--uns"])

    assert raised.value.code == 2
    assert "unrecognized arguments: --uns" in capsys.readouterr().err


@pytest.mark.parametrize("filename", AMBIGUOUS_NAMES)
def test_existing_option_shaped_path_fails_before_privileged_parsing(
    tmp_path, monkeypatch, capsys, filename
):
    """A file name must never be reinterpreted as an option merely by position."""
    (tmp_path / filename).write_text("source content long enough", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _guard_privileged_sinks(monkeypatch)

    assert _exit_code([filename]) == 2

    diagnostic = capsys.readouterr().err
    _assert_safe_operand_guidance(diagnostic)


@pytest.mark.parametrize("filename", AMBIGUOUS_NAMES)
def test_explicit_double_dash_accepts_option_shaped_path_as_operand(
    tmp_path, monkeypatch, capsys, filename
):
    """The documented boundary must make every ambiguous name usable as a path."""
    source = tmp_path / filename
    source.write_text("source content long enough", encoding="utf-8")
    seen: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_snapshot_input", _passthrough_snapshot)
    monkeypatch.setattr(
        cli,
        "convert",
        lambda path, **kwargs: (
            seen.append(path)
            or SimpleNamespace(markdown=MARKDOWN, backend="plain")
        ),
    )

    assert cli.main(["--stdout", "--", filename]) == 0

    captured = capsys.readouterr()
    assert captured.out == MARKDOWN
    assert captured.err == ""
    assert [path.name for path in seen] == [filename]


@pytest.mark.parametrize(
    ("filename", "operand"),
    (("report.txt", "report.txt"), ("--ocr", "./--ocr")),
)
def test_ordinary_and_dot_slash_paths_remain_compatible(
    tmp_path, monkeypatch, capsys, filename, operand
):
    """Hardening must preserve normal operands and the documented ``./`` escape."""
    source = tmp_path / filename
    source.write_text("source content long enough", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_snapshot_input", _passthrough_snapshot)
    monkeypatch.setattr(
        cli,
        "convert",
        lambda path, **kwargs: SimpleNamespace(markdown=MARKDOWN, backend="plain"),
    )

    assert cli.main(["--stdout", operand]) == 0

    captured = capsys.readouterr()
    assert captured.out == MARKDOWN
    assert captured.err == ""


def test_dangling_option_shaped_symlink_is_detected_by_lstat_and_sanitized(
    tmp_path, monkeypatch, capsys
):
    """Following a dangling link would miss it and expose its control character."""
    filename = "--ocr\u202e"
    link = tmp_path / filename
    try:
        link.symlink_to("missing-target")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    monkeypatch.chdir(tmp_path)
    _guard_privileged_sinks(monkeypatch)

    assert _exit_code([filename]) == 2

    diagnostic = capsys.readouterr().err
    assert "\u202e" not in diagnostic
    assert "\\u202e" in diagnostic
    _assert_safe_operand_guidance(diagnostic)


def test_option_shaped_path_lstat_failure_fails_closed_and_is_sanitized(
    tmp_path, monkeypatch, capsys
):
    """An uninspectable option-shaped token must not reach argparse."""
    filename = "--ocr\u202e"
    monkeypatch.chdir(tmp_path)

    def deny_lstat(path):
        raise PermissionError(errno.EACCES, "permission denied", path)

    monkeypatch.setattr(cli.os, "lstat", deny_lstat)

    assert _exit_code([filename]) == 2

    diagnostic = capsys.readouterr().err
    assert "\u202e" not in diagnostic
    assert "\\u202e" in diagnostic
    _assert_safe_operand_guidance(diagnostic)
