import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

from examples import smoke
from examples.smoke import build_command
from examples.verify import verify
from d2md.convert import SUPPORTED
from d2md.convert import convert


REPOSITORY = Path(__file__).resolve().parents[1]


def generate_manifest(tmp_path):
    output = tmp_path / "generated"
    subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "examples" / "generate.py"),
            "--output",
            str(output),
        ],
        cwd=REPOSITORY,
        check=True,
    )
    return json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )


def test_manifest_classifies_every_fixture(tmp_path):
    manifest = generate_manifest(tmp_path)
    images = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}

    for entry in manifest["documents"]:
        expected = (
            "ocr"
            if entry["file"] == "pdf-scanned.pdf"
            or entry["extension"] in images
            else "base"
        )
        assert entry["capability"] == expected


def test_base_verification_ignores_ocr_fixtures(tmp_path):
    generated = tmp_path / "generated"
    converted = tmp_path / "converted"
    generated.mkdir()
    converted.mkdir()
    manifest = {
        "documents": [
            {
                "file": "base.txt",
                "expected_text": "BASE TOKEN",
                "capability": "base",
            },
            {
                "file": "scan.png",
                "expected_text": "OCR TOKEN",
                "capability": "ocr",
            },
        ]
    }
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (converted / "base.md").write_text("BASE TOKEN", encoding="utf-8")

    passed, failed = verify(manifest_path, converted, profile="base")

    assert (passed, failed) == (1, 0)


def test_example_verifier_escapes_terminal_controls_in_manifest_filenames(
    tmp_path, capsys
):
    generated = tmp_path / "generated"
    converted = tmp_path / "converted"
    generated.mkdir()
    converted.mkdir()
    dangerous_name = "quarterly\x1b[31m\u202ereport.txt"
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "file": dangerous_name,
                        "expected_text": "D2MD EXAMPLE TOKEN",
                        "capability": "base",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    passed, failed = verify(manifest_path, converted)

    output = capsys.readouterr().out
    assert (passed, failed) == (0, 1)
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "quarterly\\x1b[31m\\u202ereport.txt" in output


def test_docling_command_has_both_explicit_modes(tmp_path):
    command = build_command(
        "docling", [tmp_path / "a.pdf"], tmp_path / "out", "cuda"
    )

    assert command[-5:] == [
        "--force",
        "--ocr",
        "--docling",
        "--device",
        "cuda",
    ]


def test_smoke_rejects_manifest_entries_outside_the_generated_directory(
    tmp_path, monkeypatch
):
    generated = tmp_path / "generated"
    generated.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("outside fixture", encoding="utf-8")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "file": "../secret.txt",
                        "extension": ".txt",
                        "expected_text": "outside fixture",
                        "capability": "base",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert smoke.main(["--generated", str(generated), "--converted", str(tmp_path / "converted")]) == 2


def test_example_generator_covers_every_supported_extension(tmp_path):
    output = tmp_path / "generated"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "examples" / "generate.py"),
            "--output",
            str(output),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["extension"] for entry in manifest["documents"]} == SUPPORTED
    stems = [Path(entry["file"]).stem for entry in manifest["documents"]]
    assert len(stems) == len(set(stems)), "example outputs would overwrite each other"
    for entry in manifest["documents"]:
        assert (output / entry["file"]).is_file(), entry["file"]
        assert "D2MD EXAMPLE" in entry["expected_text"]

    with zipfile.ZipFile(output / "ebook.epub") as archive:
        package = archive.read("OEBPS/content.opf").decode("utf-8")
    assert "d2md-example" in package


def test_generated_non_ocr_examples_convert_with_expected_text(tmp_path):
    output = tmp_path / "generated"
    subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "examples" / "generate.py"),
            "--output",
            str(output),
        ],
        cwd=REPOSITORY,
        check=True,
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    for entry in manifest["documents"]:
        path = output / entry["file"]
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}:
            continue
        if path.name == "pdf-scanned.pdf":
            continue
        result = convert(path)
        assert entry["expected_text"].casefold() in result.markdown.casefold(), path.name


def test_example_verifier_reports_missing_expected_text(tmp_path):
    generated = tmp_path / "generated"
    converted = tmp_path / "converted"
    generated.mkdir()
    converted.mkdir()
    manifest = {
        "documents": [
            {
                "file": "sample.txt",
                "extension": ".txt",
                "expected_text": "D2MD EXAMPLE TOKEN",
                "description": "test",
                "capability": "base",
            }
        ]
    }
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (converted / "sample.md").write_text("wrong output", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "examples" / "verify.py"),
            "--manifest",
            str(manifest_path),
            "--converted",
            str(converted),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "sample.txt" in completed.stdout


def test_example_verifier_accepts_ocr_inserted_marker_whitespace(tmp_path):
    generated = tmp_path / "generated"
    converted = tmp_path / "converted"
    generated.mkdir()
    converted.mkdir()
    manifest = {
        "documents": [
            {
                "file": "pdf-scanned.pdf",
                "extension": ".pdf",
                "expected_text": "D2MD EXAMPLE PDF SCAN",
                "description": "test",
                "capability": "ocr",
            }
        ]
    }
    manifest_path = generated / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (converted / "pdf-scanned.md").write_text(
        "D 2 MD EXAMPLE PDF SCAN 42", encoding="utf-8"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "examples" / "verify.py"),
            "--manifest",
            str(manifest_path),
            "--converted",
            str(converted),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "1 verified · 0 failed" in completed.stdout
