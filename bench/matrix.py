"""Plan production-route benchmark scenarios without running converters.

The benchmark has its own small, dependency-free planner so unit tests can
prove routing and device coverage without importing Docling or an OCR engine.
Actual conversion, timing, and result persistence live in ``matrix_worker``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform as platform_module
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Literal, Mapping, Sequence

if __package__:
    from bench.legacy_safe import display_text
else:
    from legacy_safe import display_text


SCHEMA_VERSION = 1
MAX_RAW_RESULT_BYTES = 1024 * 1024
MAX_FORMAT_MANIFEST_BYTES = 1024 * 1024
MAX_FORMAT_MANIFEST_DOCUMENTS = 100
PLAIN_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml"}
OFFICE_EXTENSIONS = {
    ".docx",
    ".xlsx",
    ".xls",
    ".pptx",
    ".html",
    ".htm",
    ".msg",
    ".epub",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
DOCLING_DEVICES = ("cpu", "cuda", "mps", "xpu")
DIRECT_OCR_BACKENDS = ("ocrmac", "rapidocr")

# One representative per public language claim.  The corpus can retain
# historical research inputs, but they are deliberately not benchmarked here.
LANGUAGE_SAMPLES = (
    ("en", "latin", "English"),
    ("de", "latin", "German"),
    ("vi", "latin", "Vietnamese"),
    ("th", "thai", "Thai"),
    ("ja", "japanese", "Japanese"),
    ("zh", "chinese", "Chinese (Simplified)"),
    ("zt", "chinese", "Chinese (Traditional)"),
    ("ko", "korean", "Korean"),
    ("ru", "cyrillic", "Russian"),
    ("ar", "arabic", "Arabic"),
)


class ResumeMismatch(ValueError):
    """Raised when an existing run cannot safely be combined with this one."""


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_strict_json(text: str) -> object:
    """Decode JSON without non-standard numeric constants or duplicate keys."""
    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_no_duplicate_json_keys,
    )


def load_bounded_strict_json(
    path: Path,
    *,
    max_bytes: int,
    expected_sha256: str | None = None,
) -> object:
    """Read and strictly decode a JSON file within an explicit byte limit."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise ValueError(f"cannot read JSON file: {path.name}") from error
    if len(raw) > max_bytes:
        raise ValueError(f"JSON file exceeds {max_bytes} byte limit: {path.name}")
    if (
        expected_sha256 is not None
        and hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise ValueError(f"JSON file SHA-256 does not match: {path.name}")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"JSON file is not valid UTF-8: {path.name}") from error
    try:
        return load_strict_json(text)
    except RecursionError as error:
        raise ValueError(f"JSON file exceeds parser nesting limit: {path.name}") from error
    except ValueError as error:
        raise ValueError(f"invalid JSON document: {path.name}: {error}") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fixture_file(
    root: Path,
    relative: str,
    *,
    label: str,
    basename_only: bool = False,
) -> Path:
    """Return a regular fixture file proved to be contained by ``root``."""
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"fixture path must be a non-empty string: {label}")
    candidate_relative = Path(relative)
    if (
        candidate_relative.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate_relative.parts)
        or "\\" in relative
        or (basename_only and (len(candidate_relative.parts) != 1 or "/" in relative))
    ):
        raise ValueError(f"fixture path escapes its root: {relative!r}")

    try:
        resolved_root = root.resolve(strict=True)
        candidate = root / candidate_relative
        details = candidate.lstat()
    except OSError as error:
        raise ValueError(f"fixture path is unavailable: {relative!r}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"fixture path must be a regular file: {relative!r}")
    try:
        candidate.resolve(strict=True).relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"fixture path escapes its root: {relative!r}") from error
    return candidate


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _repository_command(*arguments: str) -> str | None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def current_commit() -> str:
    """Return the checked-out commit, refusing an untraceable benchmark run."""
    commit = _repository_command("rev-parse", "HEAD")
    if commit is None:
        raise RuntimeError("benchmark needs a Git checkout")
    return commit


def current_clean_commit() -> str:
    """Return HEAD only when local source and fixture inputs are reproducible."""
    commit = current_commit()
    if _repository_command("status", "--porcelain"):
        raise RuntimeError("benchmark promotion needs a clean Git checkout")
    return commit


def _total_memory_mib() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.virtual_memory().total / (1024 * 1024)
    except Exception:
        return None


def _scenario_fixture_hashes(scenarios: Sequence["Scenario"]) -> dict[str, str]:
    """Hash inputs under public logical names, never their local locations."""
    hashes: dict[str, str] = {}
    for scenario in scenarios:
        input_key = f"input:{scenario.source}"
        hashes.setdefault(input_key, _sha256_file(scenario.input_path))
        if scenario.truth_path is not None:
            truth_key = f"truth:{scenario.source}"
            hashes.setdefault(truth_key, _sha256_file(scenario.truth_path))
    return hashes


def _run_metadata(
    *,
    commit: str,
    platform_label: str,
    device: str,
    timing_policy: object,
    fixture_hashes: Mapping[str, str],
) -> dict[str, object]:
    """Collect reproducibility facts without identities, paths, or env values."""
    dependencies = {
        name: value
        for name in ("d2md", "docling", "torch", "rapidocr", "ocrmac", "psutil")
        if (value := _package_version(name)) is not None
    }
    return {
        "commit": commit,
        "dirty_worktree": bool(_repository_command("status", "--porcelain")),
        "platform": platform_label,
        "device": device,
        "os": {
            "system": platform_module.system(),
            "release": platform_module.release(),
            "version": platform_module.version(),
            "architecture": platform_module.machine(),
        },
        "cpu_count": os.cpu_count(),
        "total_memory_mib": _total_memory_mib(),
        "python": sys.version.split()[0],
        "dependencies": dependencies,
        "benchmark_schema_version": SCHEMA_VERSION,
        "timing_policy": asdict(timing_policy),
        "fixture_hashes": dict(sorted(fixture_hashes.items())),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _write_json_atomic(
    path: Path, document: dict[str, object]
) -> dict[str, object]:
    """Replace ``path`` only after a complete JSON document reaches disk."""
    serialized_text = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    serialized = serialized_text.encode("utf-8")
    if len(serialized) > MAX_RAW_RESULT_BYTES:
        raise ValueError(
            f"JSON document exceeds {MAX_RAW_RESULT_BYTES} byte limit: {path.name}"
        )
    canonical = load_strict_json(serialized_text)
    assert isinstance(canonical, dict)

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return canonical


@dataclass
class ResultStore:
    """Append completed benchmark samples without losing an interrupted run."""

    path: Path
    document: dict[str, object]

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        fingerprint: str,
        metadata: dict[str, object],
    ) -> "ResultStore":
        if path.exists():
            raise FileExistsError(f"result already exists: {path.name}")
        document: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "configuration_fingerprint": fingerprint,
            "metadata": metadata,
            "samples": [],
        }
        canonical = _write_json_atomic(path, document)
        return cls(path=path, document=canonical)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        fingerprint: str,
        resume: bool,
    ) -> "ResultStore":
        if not resume:
            raise ValueError("opening an existing result requires resume=True")
        try:
            document = load_bounded_strict_json(path, max_bytes=MAX_RAW_RESULT_BYTES)
        except ValueError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                raise error.__cause__ from None
            raise ValueError(f"invalid raw benchmark JSON: {error}") from error
        if not isinstance(document, dict):
            raise ResumeMismatch("result has no valid JSON object")
        if document.get("schema_version") != SCHEMA_VERSION:
            raise ResumeMismatch("schema version does not match this benchmark")
        if document.get("configuration_fingerprint") != fingerprint:
            raise ResumeMismatch("configuration fingerprint does not match this benchmark")
        if not isinstance(document.get("samples"), list):
            raise ResumeMismatch("result has no valid samples list")
        return cls(path=path, document=document)

    def append_sample(self, sample: dict[str, object]) -> None:
        samples = self.document["samples"]
        assert isinstance(samples, list)
        updated = {**self.document, "samples": [*samples, sample]}
        self.document = _write_json_atomic(self.path, updated)


@dataclass(frozen=True)
class Scenario:
    """One public benchmark row and the private local input used to run it."""

    suite: Literal["format", "language"]
    source: str
    input_path: Path
    document_type: str
    method: Literal["default", "ocr", "docling", "docling+ocr"]
    device: str
    expected_backends: tuple[str, ...]
    marker: str | None = None
    language: str | None = None
    truth_path: Path | None = None

    @property
    def identifier(self) -> str:
        language = f":{self.language}" if self.language else ""
        return f"{self.suite}:{self.source}:{self.method}:{self.device}{language}"

    @property
    def uses_docling(self) -> bool:
        return self.method in {"docling", "docling+ocr"}

    def convert_kwargs(self) -> dict[str, object]:
        """Explicit production ``convert`` keyword arguments for this row."""
        return {
            "ocr": self.method in {"ocr", "docling+ocr"},
            "docling": self.uses_docling,
            "device": self.device if self.uses_docling else "auto",
            "lang": self.language,
        }

    def public_plan(self) -> dict[str, object]:
        """Serialize only reproducible, non-private scenario metadata."""
        return {
            "id": self.identifier,
            "suite": self.suite,
            "source": self.source,
            "document_type": self.document_type,
            "method": self.method,
            "device": self.device,
            "expected_backends": list(self.expected_backends),
            "language": self.language,
        }


def scenarios_for_device(
    scenarios: Sequence[Scenario], *, device: str
) -> list[Scenario]:
    """Select one platform/device result file without duplicate CPU routes."""
    if device not in DOCLING_DEVICES:
        raise ValueError(f"unsupported benchmark device: {device}")
    return [
        scenario
        for scenario in scenarios
        if (
            scenario.uses_docling and scenario.device == device
        )
        or (
            not scenario.uses_docling and device == "cpu"
        )
    ]


def _sample_scenario_id(sample: object) -> str:
    if not isinstance(sample, dict):
        raise ResumeMismatch("result has a non-object benchmark sample")
    scenario = sample.get("scenario")
    if not isinstance(scenario, dict):
        raise ResumeMismatch("result sample has no public scenario")
    identifier = scenario.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ResumeMismatch("result sample has no public scenario id")
    return identifier


def run_scenario_set(
    *,
    path: Path,
    fingerprint: str,
    metadata: dict[str, object],
    scenarios: Sequence[Scenario],
    run_one: Callable[[Scenario], dict[str, object]],
    resume: bool,
) -> ResultStore:
    """Run and atomically save only scenario rows absent from a matching run."""
    identifiers = [scenario.identifier for scenario in scenarios]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("benchmark scenario set contains duplicate identifiers")

    if path.exists():
        store = ResultStore.open(path, fingerprint=fingerprint, resume=resume)
    else:
        store = ResultStore.create(path, fingerprint=fingerprint, metadata=metadata)

    expected_plans = {scenario.identifier: scenario.public_plan() for scenario in scenarios}
    completed: set[str] = set()
    for sample in store.document["samples"]:
        identifier = _sample_scenario_id(sample)
        if sample["scenario"] != expected_plans.get(identifier):
            raise ResumeMismatch("result sample scenario plan does not match this run")
        completed.add(identifier)

    for scenario in scenarios:
        if scenario.identifier in completed:
            continue
        sample = run_one(scenario)
        if _sample_scenario_id(sample) != scenario.identifier:
            raise ValueError("worker returned a sample for another scenario")
        store.append_sample(sample)
        completed.add(scenario.identifier)
    return store


def configuration_fingerprint(
    *,
    commit: str,
    fixture_hashes: Mapping[str, str],
    settings: Mapping[str, object],
    scenarios: Sequence[Scenario],
) -> str:
    """Hash all and only the public inputs that make a run comparable."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "commit": commit,
        "fixture_hashes": dict(sorted(fixture_hashes.items())),
        "settings": settings,
        "scenarios": [
            scenario.public_plan()
            for scenario in sorted(scenarios, key=lambda item: item.identifier)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _ordered_devices(devices: Sequence[str]) -> tuple[str, ...]:
    unknown = set(devices) - set(DOCLING_DEVICES)
    if unknown:
        raise ValueError(f"unsupported Docling device(s): {', '.join(sorted(unknown))}")
    if "cpu" not in devices:
        raise ValueError("Docling benchmark devices must include cpu")
    return tuple(device for device in DOCLING_DEVICES if device in devices)


def _scenario(
    *,
    source: str,
    input_path: Path,
    document_type: str,
    method: Literal["default", "ocr", "docling", "docling+ocr"],
    device: str,
    expected_backends: tuple[str, ...],
    marker: str | None,
) -> Scenario:
    return Scenario(
        suite="format",
        source=source,
        input_path=input_path,
        document_type=document_type,
        method=method,
        device=device,
        expected_backends=expected_backends,
        marker=marker,
        language="latin" if method in {"ocr", "docling+ocr"} else None,
    )


def plan_format_scenarios(
    manifest_path: Path,
    *,
    docling_devices: Sequence[str],
) -> list[Scenario]:
    """Plan current explicit conversion routes for every generated fixture."""
    devices = _ordered_devices(docling_devices)
    manifest = load_bounded_strict_json(
        manifest_path, max_bytes=MAX_FORMAT_MANIFEST_BYTES
    )
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest is not a JSON object: {manifest_path.name}")
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise ValueError(f"manifest has no documents list: {manifest_path.name}")
    if len(documents) > MAX_FORMAT_MANIFEST_DOCUMENTS:
        raise ValueError(
            "manifest contains more than "
            f"{MAX_FORMAT_MANIFEST_DOCUMENTS} documents: {manifest_path.name}"
        )

    generated = manifest_path.parent
    validated_entries: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for entry in documents:
        if not isinstance(entry, dict):
            raise ValueError("manifest document entry must be an object")
        name = entry.get("file")
        extension = entry.get("extension")
        marker = entry.get("expected_text")
        if not isinstance(name, str) or not isinstance(extension, str) or not isinstance(
            marker, str
        ):
            raise ValueError("manifest document entry has invalid public fields")
        if name in seen_names:
            raise ValueError(f"manifest contains a duplicate fixture: {name}")
        if Path(name).suffix.lower() != extension.lower():
            raise ValueError(f"manifest fixture extension does not match: {name}")
        seen_names.add(name)
        validated_entries.append(entry)

    scenarios: list[Scenario] = []
    for entry in sorted(validated_entries, key=lambda value: str(value["file"])):
        name = entry.get("file")
        extension = entry.get("extension")
        marker = entry.get("expected_text")
        assert isinstance(name, str)
        assert isinstance(extension, str)
        assert isinstance(marker, str)
        path = _fixture_file(
            generated,
            name,
            label="manifest document",
            basename_only=True,
        )
        extension = extension.lower()
        capability = entry.get("capability", "base")
        if not isinstance(capability, str):
            raise ValueError("manifest document capability must be a string")

        if extension in PLAIN_EXTENSIONS:
            scenarios.append(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type="direct-text",
                    method="default",
                    device="cpu",
                    expected_backends=("plain",),
                    marker=marker,
                )
            )
        elif extension in OFFICE_EXTENSIONS:
            scenarios.append(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type="office-web",
                    method="default",
                    device="cpu",
                    expected_backends=("markitdown",),
                    marker=marker,
                )
            )
        elif extension == ".pdf" and capability == "base":
            scenarios.append(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type="born-digital-pdf",
                    method="default",
                    device="cpu",
                    expected_backends=("pypdfium2",),
                    marker=marker,
                )
            )
            scenarios.extend(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type="born-digital-pdf",
                    method="docling",
                    device=device,
                    expected_backends=("docling",),
                    marker=marker,
                )
                for device in devices
            )
        elif (extension == ".pdf" and capability == "ocr") or extension in IMAGE_EXTENSIONS:
            document_type = "scanned-pdf" if extension == ".pdf" else "image"
            scenarios.append(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type=document_type,
                    method="ocr",
                    device="cpu",
                    expected_backends=DIRECT_OCR_BACKENDS,
                    marker=marker,
                )
            )
            scenarios.extend(
                _scenario(
                    source=name,
                    input_path=path,
                    document_type=document_type,
                    method="docling+ocr",
                    device=device,
                    expected_backends=("docling+ocr",),
                    marker=marker,
                )
                for device in devices
            )
        else:
            raise ValueError(
                f"unsupported generated benchmark fixture: {name} ({capability})"
            )
    return scenarios


def plan_language_scenarios(
    corpus: Path,
    *,
    docling_devices: Sequence[str],
) -> list[Scenario]:
    """Plan direct and structured OCR for the ten public language rows."""
    devices = _ordered_devices(docling_devices)
    scenarios: list[Scenario] = []
    for code, script, _label in LANGUAGE_SAMPLES:
        source = f"pdf/{code}-clean.pdf"
        input_path = _fixture_file(corpus, source, label="language input")
        truth_path = _fixture_file(
            corpus,
            f"truth/{code}-clean.txt",
            label="language truth",
        )
        scenarios.append(
            Scenario(
                suite="language",
                source=source,
                input_path=input_path,
                document_type="scanned-pdf",
                method="ocr",
                device="cpu",
                expected_backends=DIRECT_OCR_BACKENDS,
                language=script,
                truth_path=truth_path,
            )
        )
        scenarios.extend(
            Scenario(
                suite="language",
                source=source,
                input_path=input_path,
                document_type="scanned-pdf",
                method="docling+ocr",
                device=device,
                expected_backends=("docling+ocr",),
                language=script,
                truth_path=truth_path,
            )
            for device in devices
        )
    return scenarios


def plan_benchmark_run(
    *,
    manifest_path: Path,
    corpus: Path | None,
    suite: Literal["format", "language", "all"],
    device: str,
    only_sources: Sequence[str] = (),
) -> tuple[list[Scenario], dict[str, str]]:
    """Build one host's scenarios and all public fixture hashes.

    The same function is used when measuring and when a later promotion
    independently rebuilds the claimed plan from trusted local fixtures.
    """
    if suite not in {"format", "language", "all"}:
        raise ValueError(f"unknown benchmark suite: {suite}")
    if device not in DOCLING_DEVICES:
        raise ValueError(f"unsupported benchmark device: {device}")

    docling_devices = ("cpu",) if device == "cpu" else ("cpu", device)
    scenarios: list[Scenario] = []
    if suite in {"format", "all"}:
        scenarios.extend(
            plan_format_scenarios(manifest_path, docling_devices=docling_devices)
        )
    if suite in {"language", "all"}:
        if corpus is None:
            raise ValueError("language or all suite requires a corpus directory")
        scenarios.extend(
            plan_language_scenarios(corpus, docling_devices=docling_devices)
        )
    selected = scenarios_for_device(scenarios, device=device)
    if only_sources:
        requested = set(only_sources)
        selected = [scenario for scenario in selected if scenario.source in requested]
        missing = requested - {scenario.source for scenario in selected}
        if missing:
            raise ValueError(
                "no selected scenario for source(s): " + ", ".join(sorted(missing))
            )
    if not selected:
        raise ValueError(f"no scenarios selected for device {device}")

    fixture_hashes = {
        "manifest": _sha256_file(manifest_path),
        **_scenario_fixture_hashes(scenarios),
    }
    return selected, fixture_hashes


def benchmark_fingerprint(
    *,
    manifest_path: Path,
    corpus: Path | None,
    suite: Literal["format", "language", "all"],
    device: str,
    commit: str,
    timing_policy: Mapping[str, object],
    only_sources: Sequence[str] = (),
) -> tuple[list[Scenario], dict[str, str], str]:
    """Rebuild the public configuration fingerprint from local trusted inputs."""
    selected, fixture_hashes = plan_benchmark_run(
        manifest_path=manifest_path,
        corpus=corpus,
        suite=suite,
        device=device,
        only_sources=only_sources,
    )
    fingerprint = configuration_fingerprint(
        commit=commit,
        fixture_hashes=fixture_hashes,
        settings={"suite": suite, "timing_policy": dict(timing_policy)},
        scenarios=selected,
    )
    return selected, fixture_hashes, fingerprint


def run_planned_benchmark(
    *,
    manifest_path: Path,
    corpus: Path | None,
    suite: Literal["format", "language", "all"],
    platform_label: str,
    device: str,
    output_path: Path,
    commit: str | None = None,
    run_one: Callable[[Scenario], dict[str, object]] | None = None,
    timing_policy: object,
    only_sources: Sequence[str] = (),
    resume: bool,
) -> ResultStore:
    """Plan, fingerprint, run, and persist one platform/device result file."""
    measured_commit = current_commit()
    if commit is not None and commit != measured_commit:
        raise ValueError("benchmark commit override must match HEAD")
    active_commit = measured_commit
    selected, fixture_hashes, fingerprint = benchmark_fingerprint(
        manifest_path=manifest_path,
        corpus=corpus,
        suite=suite,
        device=device,
        commit=active_commit,
        timing_policy=asdict(timing_policy),
        only_sources=only_sources,
    )
    metadata = _run_metadata(
        commit=active_commit,
        platform_label=platform_label,
        device=device,
        timing_policy=timing_policy,
        fixture_hashes=fixture_hashes,
    )
    metadata.update(
        {
            "purpose": "smoke" if only_sources else "production",
            "suite": suite,
            "scenario_plan": [scenario.public_plan() for scenario in selected],
        }
    )
    if run_one is None:
        from bench.matrix_worker import run_scenario

        def run_one(scenario: Scenario) -> dict[str, object]:
            return run_scenario(scenario, policy=timing_policy)

    return run_scenario_set(
        path=output_path,
        fingerprint=fingerprint,
        metadata=metadata,
        scenarios=selected,
        run_one=run_one,
        resume=resume,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark d2md's current explicit production routes."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="write one resumable platform/device result")
    run.add_argument(
        "--format-manifest",
        type=Path,
        default=Path("examples/generated/manifest.json"),
        help="generated format fixture manifest",
    )
    run.add_argument(
        "--corpus",
        type=Path,
        default=Path("corpus"),
        help="OCR corpus directory (required for language/all)",
    )
    run.add_argument(
        "--suite", choices=("format", "language", "all"), default="all"
    )
    run.add_argument("--platform", required=True, help="public machine label")
    run.add_argument("--device", choices=DOCLING_DEVICES, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--only-source",
        action="append",
        default=[],
        help="limit a smoke run to one public fixture source (repeatable)",
    )
    run.add_argument("--resume", action="store_true")
    run.add_argument("--warm-repeats", type=int, default=3)
    run.add_argument("--max-warm-repeats", type=int, default=7)
    run.add_argument("--min-sample-seconds", type=float, default=0.25)
    run.add_argument("--variance-ratio", type=float, default=0.10)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark CLI without making it a runtime package command."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.command != "run":  # pragma: no cover - argparse keeps this closed
        parser.error(f"unknown command: {args.command}")
    try:
        from bench.matrix_worker import TimingPolicy

        timing_policy = TimingPolicy(
            warm_repeats=args.warm_repeats,
            max_warm_repeats=args.max_warm_repeats,
            min_sample_seconds=args.min_sample_seconds,
            variance_ratio=args.variance_ratio,
        )
        store = run_planned_benchmark(
            manifest_path=args.format_manifest,
            corpus=args.corpus if args.suite in {"language", "all"} else None,
            suite=args.suite,
            platform_label=args.platform,
            device=args.device,
            output_path=args.output,
            commit=None,
            timing_policy=timing_policy,
            only_sources=args.only_source,
            resume=args.resume,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        parser.error(display_text(error))
    samples = store.document["samples"]
    assert isinstance(samples, list)
    print(f"wrote {len(samples)} benchmark samples to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI itself
    raise SystemExit(main())
