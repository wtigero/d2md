from types import SimpleNamespace

import pytest

from d2md import cli


@pytest.mark.parametrize("device", ("auto", "cpu", "cuda", "mps", "xpu"))
def test_cli_forwards_each_public_device(tmp_path, monkeypatch, device):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    seen = []

    def fake_convert(path, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(markdown="converted output long enough", backend="docling")

    monkeypatch.setattr(cli, "convert", fake_convert)
    monkeypatch.setattr(cli, "ensure_docling_available", lambda: None)

    options = [str(source)]
    if device != "auto":
        options.append("--docling")
    options.extend(["--device", device, "-o", str(tmp_path / "out")])

    exit_code = cli.main(options)

    assert exit_code == 0
    assert seen == [
        {
            "lang": None,
            "limits": cli.DEFAULT_LIMITS,
            "device": device,
            "identity": cli._file_identity(source.lstat()),
            "root": None,
            "ocr": False,
            "docling": device != "auto",
        }
    ]


def test_cli_defaults_to_auto_device(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    seen = []
    monkeypatch.setattr(
        cli,
        "convert",
        lambda path, **kwargs: (
            seen.append(kwargs)
            or SimpleNamespace(
                markdown="converted output long enough", backend="docling"
            )
        ),
    )

    assert cli.main([str(source), "-o", str(tmp_path / "out")]) == 0
    assert seen == [
        {
            "lang": None,
            "limits": cli.DEFAULT_LIMITS,
            "device": "auto",
            "ocr": False,
            "docling": False,
            "identity": cli._file_identity(source.lstat()),
            "root": None,
        }
    ]


def test_cli_rejects_generic_gpu():
    with pytest.raises(SystemExit) as raised:
        cli.main(["scan.pdf", "--device", "gpu"])

    assert raised.value.code == 2
