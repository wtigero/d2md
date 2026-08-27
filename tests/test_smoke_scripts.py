import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]


def run_bash(*args):
    return subprocess.run(
        [
            shutil.which("bash"),
            str(REPOSITORY / "scripts" / "smoke-linux.sh"),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="Bash is unavailable on this platform",
)
def test_linux_smoke_rejects_unknown_device_before_setup():
    completed = run_bash("--device", "gpu")

    assert completed.returncode == 2
    assert "--device auto|cpu|cuda|mps|xpu" in completed.stderr


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="Bash is unavailable on this platform",
)
def test_linux_rejects_device_outside_docling():
    completed = run_bash("--profile", "base", "--device", "cuda")

    assert completed.returncode == 2
    assert "--device requires --profile docling" in completed.stderr


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def test_windows_import_probe_uses_powershell_safe_python_code():
    script = (REPOSITORY / "scripts" / "smoke-windows.ps1").read_text(
        encoding="utf-8"
    )

    assert "-c 'import d2md; print(d2md.__file__)'" in script
    assert "Imported d2md from:" in script
    assert "Imported doc2md from:" not in script


def test_linux_import_probe_reports_the_d2md_package():
    script = (REPOSITORY / "scripts" / "smoke-linux.sh").read_text(
        encoding="utf-8"
    )

    assert "import d2md" in script
    assert "Imported d2md from:" in script
    assert "Imported doc2md from:" not in script


def run_powershell(*args):
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REPOSITORY / "scripts" / "smoke-windows.ps1"),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_smoke_rejects_unknown_device_before_setup():
    completed = run_powershell("-Device", "gpu", "-SkipIntegration")

    assert completed.returncode != 0
    assert "gpu" in completed.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_windows_rejects_device_outside_docling():
    completed = run_powershell(
        "-Profile", "Base", "-Device", "cuda", "-SkipIntegration"
    )

    assert completed.returncode != 0
    assert "Docling" in completed.stderr
