import pytest

from d2md import capabilities
from d2md.errors import ConversionError


def test_install_commands_target_the_pypi_project():
    assert capabilities.install_command("ocr", force=False) == (
        'uv tool install "d2md[ocr]"'
    )
    assert capabilities.install_command("docling", force=True) == (
        'uv tool install --force "d2md[docling]"'
    )


def test_missing_ocr_is_actionable(monkeypatch):
    monkeypatch.setattr(capabilities, "available_engines", lambda: [])
    with pytest.raises(ConversionError, match=r"d2md\[ocr\].*--ocr"):
        capabilities.ensure_ocr_available()


def test_missing_docling_is_actionable(monkeypatch):
    monkeypatch.setattr(capabilities, "module_available", lambda name: False)
    with pytest.raises(ConversionError, match=r"d2md\[docling\].*--docling"):
        capabilities.ensure_docling_available()
