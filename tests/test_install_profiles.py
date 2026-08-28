import re
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
RELEASE_BLOB_URL = "https://github.com/wtigero/d2md/blob/v0.1.1/"
HISTORICAL_RESULTS = (
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/README.md",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/macos-arm64-cpu.json",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/macos-arm64-mps.json",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/summary.csv",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/ubuntu-gtx1060-cpu.json",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/ubuntu-gtx1060-cuda.json",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/windows-rtx3090ti-cpu.json",
    "bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/windows-rtx3090ti-cuda.json",
)


def _relative_cross_file_links(markdown):
    targets = re.findall(r"\]\(([^)]+)\)", markdown)
    return [
        target
        for target in targets
        if not target.startswith("#") and "://" not in target
    ]


def test_project_identity_is_d2md():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = config["project"]

    assert project["name"] == "d2md"
    assert project["scripts"] == {"d2md": "d2md.cli:main"}
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"][
        "packages"
    ] == ["src/d2md"]
    assert set(project["urls"].values()) == {
        "https://github.com/wtigero/d2md",
        "https://github.com/wtigero/d2md/issues",
        "https://github.com/wtigero/d2md/blob/main/CHANGELOG.md",
        "https://github.com/wtigero/d2md/security/policy",
    }
    assert (ROOT / "src" / "d2md").is_dir()
    assert not (ROOT / "src" / "doc2md").exists()


def test_base_excludes_heavy_stacks():
    base = "\n".join(PROJECT["dependencies"]).casefold()
    for name in ("docling", "torch", "transformers", "ocrmac", "rapidocr"):
        assert name not in base
    assert "pypdfium2" in base and "markitdown" in base


def test_optional_groups_are_complete():
    extras = PROJECT["optional-dependencies"]
    assert extras["ocr"] == [
        "pillow>=10",
        "ocrmac>=1,<2; sys_platform == 'darwin'",
        "rapidocr>=3,<4; sys_platform != 'darwin'",
        "numpy>=1.24; sys_platform != 'darwin'",
    ]
    assert set(extras["ocr"]).issubset(extras["docling"])
    assert "docling>=2.119,<3" in extras["docling"]
    assert "transformers<5.15" in extras["docling"]


def test_dev_dependencies_include_python_310_tomllib_backport():
    assert "tomli>=2; python_version < '3.11'" in PROJECT[
        "optional-dependencies"
    ]["dev"]


def test_readme_orders_base_ocr_and_docling():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.index("## Quick start") < readme.index(
        "## OCR scanned documents"
    )
    assert readme.index("## OCR scanned documents") < readme.index(
        "## Layout and tables with Docling"
    )
    for command in (
        "uv tool install d2md",
        "pip install d2md",
        'uv tool install "d2md[ocr]"',
        'uv tool install "d2md[docling]"',
        "d2md report.pdf --stdout",
    ):
        assert command in readme
    assert readme.index("uv tool install d2md") < readme.index("git+https://")


def test_readme_removes_old_architecture_claims():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for stale in ("~500x", "500×", "129 tests", "0.4s a page"):
        assert stale not in readme
    assert "hindi" not in readme.casefold()


def test_readme_documents_verified_gpu_installation():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '--torch-backend auto "d2md[docling]"' in readme
    assert '--torch-backend cu126 "d2md[docling]"' in readme
    assert "torch and torchvision" in readme
    assert (
        "[manual verification results]("
        f"{RELEASE_BLOB_URL}docs/verification.md)"
    ) in readme


def test_readme_cross_file_links_are_absolute_release_urls():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)]+)\)", readme)

    assert _relative_cross_file_links(readme) == []
    assert [target for target in targets if target.startswith(RELEASE_BLOB_URL)] == [
        f"{RELEASE_BLOB_URL}bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/README.md",
        f"{RELEASE_BLOB_URL}docs/verification.md#hardware-and-relevant-packages",
        f"{RELEASE_BLOB_URL}docs/release-process.md",
        f"{RELEASE_BLOB_URL}examples/README.md",
        f"{RELEASE_BLOB_URL}docs/verification.md",
        f"{RELEASE_BLOB_URL}bench/README.md",
        f"{RELEASE_BLOB_URL}bench/results/7203499e58bf8e6415b3190638d0f8a689f55924/README.md",
        f"{RELEASE_BLOB_URL}docs/findings.md",
        f"{RELEASE_BLOB_URL}docs/ocr.md",
    ]


def test_readme_link_contract_allows_same_page_and_absolute_links():
    markdown = "[Section](#installation) [Release](https://example.test/release)"

    assert _relative_cross_file_links(markdown) == []


def test_sdist_keeps_historical_benchmark_evidence():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    excluded = config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]

    assert "/bench/results" not in excluded
    assert all((ROOT / path).is_file() for path in HISTORICAL_RESULTS)


def test_manual_verification_report_has_all_profiles_and_platforms():
    report = (ROOT / "docs" / "verification.md").read_text(encoding="utf-8")
    for platform in ("macOS 26.5.2", "Ubuntu 24.04.4", "Windows 11 Pro"):
        assert platform in report
    for profile in ("Base", "OCR", "Docling CPU", "Accelerator"):
        assert profile in report
    assert "`ec8cae7`" in report
    assert "162 passed, 2 skipped" in report
    assert "Model caches were not purged" in report
    assert "performance benchmark" in report
