"""Security regressions for the legacy OCR benchmark scripts."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

import bench.gaps as legacy_gaps
import bench.matrix as benchmark_matrix
import bench.report as legacy_report
import bench.run as legacy_run
import bench.shipped as legacy_shipped
import bench.support as legacy_support
import bench.surya_run as legacy_surya
import bench.vlm as legacy_vlm


REPOSITORY = Path(__file__).resolve().parents[1]


def _write_result(
    corpus: Path,
    *,
    file: object = "th-clean",
    engine: object = "legacy-engine",
    variant: object = "clean",
    pred: str = "สวัสดี",
) -> None:
    corpus.mkdir(exist_ok=True)
    (corpus / "results-legacy.json").write_text(
        json.dumps(
            {
                "engine": engine,
                "rows": [
                    {
                        "file": file,
                        "script": "th",
                        "variant": variant,
                        "mode": "told",
                        "cer_ns": 0.0,
                        "cer_bag": 0.0,
                        "secs": 0.1,
                        "pred": pred,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _rescore(module, corpus: Path) -> int:
    argv = [str(module.__file__), str(corpus), "--mode", "told"]
    if module is legacy_report:
        argv.append("--rescore")
    return module.main(argv)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
@pytest.mark.parametrize(
    "truth_identifier",
    (
        "../outside",
        "bad/name",
        "bad\\name",
        "C:secret",
        "bad\x1bname",
        7,
        "",
        ".",
        "..",
    ),
)
def test_legacy_rescore_rejects_truth_identifiers_outside_the_producer_contract(
    tmp_path, module, truth_identifier
):
    corpus = tmp_path / "corpus"
    truth = corpus / "truth"
    truth.mkdir(parents=True)
    (corpus / "outside.txt").write_text("outside", encoding="utf-8")
    _write_result(corpus, file=truth_identifier)

    with pytest.raises(ValueError, match="truth identifier"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_rescore_rejects_absolute_truth_identifier(tmp_path, module):
    corpus = tmp_path / "corpus"
    (corpus / "truth").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.with_suffix(".txt").write_text("outside", encoding="utf-8")
    _write_result(corpus, file=str(outside))

    with pytest.raises(ValueError, match="truth identifier"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_rescore_rejects_symlinked_truth_file(tmp_path, module):
    corpus = tmp_path / "corpus"
    truth = corpus / "truth"
    truth.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (truth / "th-clean.txt").symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    _write_result(corpus)

    with pytest.raises(ValueError, match="regular non-symlink"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_rescore_accepts_a_regular_truth_stem(tmp_path, module, capsys):
    corpus = tmp_path / "corpus"
    truth = corpus / "truth"
    truth.mkdir(parents=True)
    (truth / "th-clean.txt").write_text("สวัสดี", encoding="utf-8")
    _write_result(corpus)

    assert _rescore(module, corpus) == 0
    assert "legacy-engine" in capsys.readouterr().out


def _write_runner_corpus(tmp_path: Path, stem: str) -> Path:
    corpus = tmp_path / "corpus"
    (corpus / "pdf").mkdir(parents=True)
    (corpus / "truth").mkdir()
    try:
        (corpus / "pdf" / f"{stem}.pdf").write_bytes(b"pdf")
        (corpus / "truth" / f"{stem}.txt").write_text("ไทย", encoding="utf-8")
    except OSError as error:
        pytest.skip(f"control-character filenames are unavailable: {error}")
    return corpus


def _assert_visible_escapes(output: str) -> None:
    for control in ("\x1b", "\x7f", "\x85", "\u202e"):
        assert control not in output
    assert "ไทย" in output
    assert "\\x1b" in output
    assert "\\x7f" in output
    assert "\\x85" in output
    assert "\\u202e" in output


def test_legacy_run_escapes_corpus_stem_and_truncated_exception(
    tmp_path, monkeypatch, capsys
):
    stem = "th-ไทย\x1b[31m\x7f\x85\u202e"
    corpus = _write_runner_corpus(tmp_path, stem)

    def fail(_path, _script):
        raise RuntimeError("x" * 29 + "\x1b[31m")

    monkeypatch.setattr(legacy_run, "make_runner", lambda _name: fail)

    assert legacy_run.main(
        [str(legacy_run.__file__), str(corpus), "pypdfium2", "--told"]
    ) == 0

    output = capsys.readouterr().out
    _assert_visible_escapes(output)
    assert "x" * 29 + "\\x1b" in output


def test_legacy_gaps_escapes_corpus_stem_and_exception(
    tmp_path, monkeypatch, capsys
):
    stem = "th-ไทย\x1b[31m\x7f\x85\u202e"
    corpus = _write_runner_corpus(tmp_path, stem)

    def fail(_image, _scripts):
        raise RuntimeError("ไทย\x1b]8;;https://invalid\x07\x85\u202e")

    monkeypatch.setitem(legacy_gaps.READERS, "vision", lambda: fail)
    monkeypatch.setattr(legacy_gaps, "_page_images", lambda _path: [object()])

    assert legacy_gaps.main(
        [str(legacy_gaps.__file__), str(corpus), "vision", "th"]
    ) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_gaps_escapes_ocr_prediction_preview(
    tmp_path, monkeypatch, capsys
):
    corpus = _write_runner_corpus(tmp_path, "th-clean")
    prediction = "ไทย\x1b[31m\x7f\x85\u202e"

    def read(_image, scripts):
        return {scripts[0]: (prediction, 1.0)}

    monkeypatch.setitem(legacy_gaps.READERS, "vision", lambda: read)
    monkeypatch.setattr(legacy_gaps, "_page_images", lambda _path: [object()])

    assert legacy_gaps.main(
        [str(legacy_gaps.__file__), str(corpus), "vision", "th"]
    ) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_shipped_escapes_corpus_stem_and_exception(
    tmp_path, monkeypatch, capsys
):
    stem = "th-ไทย\x1b[31m\x7f\x85\u202e"
    corpus = _write_runner_corpus(tmp_path, stem)

    def fail(_path):
        raise RuntimeError("ไทย\x1b]8;;https://invalid\x07\x85\u202e")

    monkeypatch.setitem(legacy_shipped.READERS, "test-engine", lambda *_args: None)
    monkeypatch.setitem(
        legacy_shipped.ENGINE_SCRIPTS, "test-engine", {"thai": "th"}
    )
    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=fail)
    )

    assert legacy_shipped.main(
        [str(legacy_shipped.__file__), str(corpus), "test-engine"]
    ) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_surya_escapes_corpus_stem_and_exception(
    tmp_path, monkeypatch, capsys
):
    stem = "th-ไทย\x1b[31m\x7f\x85\u202e"
    corpus = _write_runner_corpus(tmp_path, stem)

    recognition_module = ModuleType("surya.recognition")
    recognition_module.RecognitionPredictor = lambda: object()
    monkeypatch.setitem(sys.modules, "surya", ModuleType("surya"))
    monkeypatch.setitem(sys.modules, "surya.recognition", recognition_module)

    def fail(_path):
        raise RuntimeError("ไทย\x1b]8;;https://invalid\x07\x85\u202e")

    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=fail)
    )

    assert legacy_surya.main(
        [str(legacy_surya.__file__), str(corpus), "th"]
    ) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_vlm_escapes_spec_name_and_converter_exception(
    tmp_path, monkeypatch, capsys
):
    hostile = "ไทย\x1b[31m\x7f\x85\u202e"

    def fail(_spec):
        raise RuntimeError(hostile)

    monkeypatch.setattr(legacy_vlm, "converter", fail)

    assert legacy_vlm.main([str(legacy_vlm.__file__), str(tmp_path), hostile]) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_vlm_escapes_conversion_exception(tmp_path, monkeypatch, capsys):
    corpus = _write_runner_corpus(tmp_path, "en-clean")
    hostile = "ไทย\x1b[31m\x7f\x85\u202e"

    def fail(_path):
        raise RuntimeError(hostile)

    monkeypatch.setattr(
        legacy_vlm,
        "converter",
        lambda _spec: SimpleNamespace(convert=fail),
    )

    assert legacy_vlm.main(
        [str(legacy_vlm.__file__), str(corpus), "SAFE_SPEC"]
    ) == 0

    _assert_visible_escapes(capsys.readouterr().out)


def test_legacy_vlm_escapes_result_destination(tmp_path, monkeypatch, capsys):
    corpus = _write_runner_corpus(tmp_path, "en-clean")
    hostile_spec = "ไทย\x1b[31m\x7f\x85\u202e"
    converted = SimpleNamespace(
        document=SimpleNamespace(export_to_markdown=lambda: "ไทย")
    )
    monkeypatch.setattr(
        legacy_vlm,
        "converter",
        lambda _spec: SimpleNamespace(convert=lambda _path: converted),
    )
    writes = []

    def capture_result(path, text, *, encoding=None):
        writes.append((path, text, encoding))
        return len(text)

    monkeypatch.setattr(Path, "write_text", capture_result)

    assert legacy_vlm.main(
        [str(legacy_vlm.__file__), str(corpus), hostile_spec]
    ) == 0

    output = capsys.readouterr().out
    _assert_visible_escapes(output)
    assert "built in" in output
    assert "→" in output
    assert len(writes) == 1
    destination, serialized, encoding = writes[0]
    assert destination == corpus / f"results-vlm-{hostile_spec}.json"
    assert json.loads(serialized)["engine"] == hostile_spec
    assert encoding == "utf-8"


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_result_reader_rejects_oversized_json_before_parsing(
    tmp_path, module
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "results-oversized.json").write_bytes(
        b" " * (benchmark_matrix.MAX_RAW_RESULT_BYTES + 1)
    )

    with pytest.raises(ValueError, match="exceeds.*byte limit"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_result_reader_rejects_duplicate_json_fields(tmp_path, module):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "results-duplicate.json").write_text(
        '{"engine":"first","engine":"second","rows":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
@pytest.mark.parametrize("attack", ("duplicate-key", "result-filename"))
def test_legacy_script_stderr_never_replays_unsanitized_loader_cause(
    tmp_path, module, attack
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    hostile = "ไทย\x1b]8;;https://invalid\x07\x85\u202e"
    if attack == "duplicate-key":
        encoded_key = json.dumps(hostile)
        (corpus / "results-hostile.json").write_text(
            f'{{"engine":"legacy","rows":[],{encoded_key}:1,{encoded_key}:2}}',
            encoding="utf-8",
        )
        explanation = "duplicate JSON object key"
    else:
        hostile = "ไทย\x1b]31m\x07\x85\u202e"
        try:
            (corpus / f"results-{hostile}.json").write_bytes(b'"\xff"')
        except OSError as error:
            pytest.skip(f"control-character filenames are unavailable: {error}")
        explanation = "not valid UTF-8"

    completed = subprocess.run(
        [sys.executable, str(module.__file__), str(corpus)],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert explanation in completed.stderr
    for control in ("\x1b", "\x07", "\x85", "\u202e"):
        assert control not in completed.stderr
    for visible_escape in ("\\x1b", "\\x07", "\\x85", "\\u202e"):
        assert visible_escape in completed.stderr


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("engine", ["legacy-engine"]),
        ("variant", {"name": "clean"}),
    ),
)
def test_legacy_result_reader_rejects_non_string_table_labels(
    tmp_path, module, field, value
):
    corpus = tmp_path / "corpus"
    truth = corpus / "truth"
    truth.mkdir(parents=True)
    (truth / "th-clean.txt").write_text("สวัสดี", encoding="utf-8")
    kwargs = {field: value}
    _write_result(corpus, **kwargs)

    with pytest.raises(ValueError, match=rf"{field}.*string"):
        _rescore(module, corpus)


@pytest.mark.parametrize("module", (legacy_report, legacy_support))
def test_legacy_tables_encode_result_controlled_markdown_labels(
    tmp_path, module, capsys
):
    corpus = tmp_path / "corpus"
    truth = corpus / "truth"
    truth.mkdir(parents=True)
    (truth / "th-clean.txt").write_text("สวัสดี", encoding="utf-8")
    payload = "ไทย\n| \\ [link](url) ![image](url) <img> `code`\r\u202e"
    _write_result(corpus, engine=payload, variant=payload)

    assert _rescore(module, corpus) == 0

    output = capsys.readouterr().out
    assert "ไทย" in output
    for marker in (
        "\n| \\ [link]",
        "\\",
        "[link]",
        "![image]",
        "<img>",
        "`code`",
        "\r",
        "\u202e",
    ):
        assert marker not in output
    for encoded in (
        "&#92;x0a",
        "&#124;",
        "&#92;",
        "&#91;",
        "&#93;",
        "&#40;",
        "&#41;",
        "&#33;",
        "&lt;",
        "&gt;",
        "&#96;",
        "&#92;x0d",
        "&#92;u202e",
    ):
        assert encoded in output
