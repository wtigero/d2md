"""Validate and render public evidence from production benchmark JSON files."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from io import StringIO
import json
import math
from pathlib import Path
import re
import statistics
import unicodedata
from collections.abc import Iterable, Sequence
from urllib.parse import unquote

from bench.legacy_safe import display_text
from bench.matrix import (
    MAX_RAW_RESULT_BYTES,
    SCHEMA_VERSION,
    benchmark_fingerprint,
    current_clean_commit,
    load_bounded_strict_json,
)


class ValidationError(ValueError):
    """Raised when raw benchmark output is not safe or complete to publish."""


MAX_PUBLIC_TEXT_LENGTH = 4096
MAX_PUBLIC_NESTING = 64
PUBLIC_TEXT_SEPARATORS = {"\u2028", "\u2029"}
PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
DECODED_MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})[A-Za-z0-9]{2}")
URL_OR_URI = re.compile(
    r"""(?ix)
    (?:
        (?<![A-Za-z0-9_.+.-])[A-Za-z][A-Za-z0-9+.-]{1,31}\s*://
        |
        (?<![A-Za-z0-9_.+.-])
        (?:https?|ftps?|sftp|ssh|file|data|mailto|urn|wss?|ldaps?|tel|news|
           nntp|gopher|magnet|ipfs|ipns|git|svn)\s*:
    )
    """
)
PRIVATE_PATH = re.compile(
    r"""(?ix)
    (?:
        (?:^|(?<=[^A-Za-z0-9]))//[^\s]*
        | (?:^|(?<=[^A-Za-z0-9]))/(?!/)
        | (?:^|(?<=[^A-Za-z0-9]))
          ~(?:$|[\\/]|[A-Za-z0-9_.+-]+(?:$|[\\/]))
        | (?<![A-Za-z0-9])[A-Za-z]:(?:$|[^\s]*)
        | (?:^|(?<=[^A-Za-z0-9]))\\(?!\\)[^\s]*
        | \\\\[^\s]*
        | (?<![A-Za-z0-9_.+.-])file\s*:
        | %(?:2f|5c)
    )
    """
)
SHA256 = re.compile(r"[0-9a-f]{64}")
TRUSTED_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)")
PLATFORM = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}")
STARTED_AT_UTC = re.compile(r".+(?:Z|[+-]00:00)")
SCENARIO_SUITES = {"format", "language"}
SCENARIO_METHODS = {"default", "ocr", "docling", "docling+ocr"}
SCENARIO_DEVICES = {"cpu", "cuda", "mps", "xpu"}
DOCUMENT_TYPES = {
    "born-digital-pdf",
    "direct-text",
    "image",
    "office-web",
    "scanned-pdf",
}
LANGUAGES = {"arabic", "chinese", "cyrillic", "japanese", "korean", "latin", "thai"}
BACKENDS = {"docling", "docling+ocr", "markitdown", "ocrmac", "plain", "pypdfium2", "rapidocr"}
PROCESS_RSS_STATUSES = {"available", "psutil-unavailable", "rss-unavailable"}
PYTORCH_ALLOCATOR_STATUSES = {
    "available",
    "allocator-peak-unavailable",
    "device-unavailable",
    "mps-has-no-peak-allocator-api",
    "not-applicable-for-cpu",
    "peak-allocator-api-unavailable",
    "pytorch-unavailable",
    "unable-to-reset-allocator-peak",
    "unsupported-device",
}
FORBIDDEN_KEYS = {
    "hostname",
    "username",
    "environment",
    "env",
    "input_path",
    "truth_path",
    "markdown",
    "prediction",
    "truth",
}
DOCUMENT_KEYS = {
    "schema_version",
    "configuration_fingerprint",
    "metadata",
    "samples",
}
SCENARIO_KEYS = {
    "id",
    "suite",
    "source",
    "document_type",
    "method",
    "device",
    "expected_backends",
    "language",
}
SUCCESS_SAMPLE_KEYS = {
    "scenario",
    "status",
    "backend",
    "marker_verified",
    "output_sha256",
    "initialization_seconds",
    "warm_samples_seconds",
    "warm_operation_counts",
    "warm_median_seconds",
    "warm_range_seconds",
    "adaptive_warm_repeats",
    "resources",
    "quality",
}
UNSUPPORTED_SAMPLE_KEYS = {
    "scenario",
    "status",
    "error_type",
    "error_message",
}
FAILED_SAMPLE_KEYS = SUCCESS_SAMPLE_KEYS | {
    "expected_backends",
    "error_type",
    "error_message",
}
METADATA_KEYS = {
    "benchmark_schema_version",
    "commit",
    "cpu_count",
    "dependencies",
    "device",
    "dirty_worktree",
    "fixture_hashes",
    "os",
    "platform",
    "purpose",
    "python",
    "scenario_plan",
    "started_at_utc",
    "suite",
    "timing_policy",
    "total_memory_mib",
}
TIMING_POLICY_KEYS = {
    "warm_repeats",
    "max_warm_repeats",
    "min_sample_seconds",
    "variance_ratio",
}
PRODUCTION_TIMING_POLICY = {
    "warm_repeats": 3,
    "max_warm_repeats": 7,
    "min_sample_seconds": 0.25,
    "variance_ratio": 0.10,
}
OS_KEYS = {"system", "release", "version", "architecture"}
CURRENT_DEPENDENCY_KEYS = {
    "d2md",
    "docling",
    "torch",
    "rapidocr",
    "ocrmac",
    "psutil",
}
HISTORICAL_BENCHMARK_COMMIT = "7203499e58bf8e6415b3190638d0f8a689f55924"
HISTORICAL_PROJECT_DEPENDENCY_KEYS = {"doc2md"}
HISTORICAL_DEPENDENCY_KEYS = (
    CURRENT_DEPENDENCY_KEYS - {"d2md"}
) | HISTORICAL_PROJECT_DEPENDENCY_KEYS
RESOURCE_KEYS = {
    "peak_process_rss_mib",
    "process_rss_status",
    "pytorch_allocator_peak_mib",
    "pytorch_allocator_peak_status",
}
QUALITY_KEYS = {"cer", "cer_ns", "cer_bag"}


def _fail(message: str) -> None:
    raise ValidationError(message)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _only_keys(value: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(f"unknown {label} field: {unknown[0]}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _finite_nonnegative(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(f"{label} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        _fail(f"{label} must be finite and nonnegative")
    return numeric


def _nullable_finite_nonnegative(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_nonnegative(value, label)


def _finite_positive(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(f"{label} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        _fail(f"{label} must be finite and positive")
    return numeric


def _public_values(value: object, *, depth: int = 0) -> Iterable[str]:
    if depth > MAX_PUBLIC_NESTING:
        _fail("public data exceeds the maximum nesting depth")
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                _fail("public object key must be a string")
            if child_key in FORBIDDEN_KEYS:
                _fail(f"forbidden private key: {child_key}")
            yield child_key
            yield from _public_values(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _public_values(child, depth=depth + 1)
    elif isinstance(value, str):
        yield value


def _normalize_percent_encoding(value: str) -> str:
    """Decode valid nested percent escapes with an input-derived bound.

    Literal percent text is public metadata, not an encoding error. Only complete
    ``%HH`` octets are decoded; any resulting escape is handled by the next pass.
    """
    if len(value) > MAX_PUBLIC_TEXT_LENGTH:
        _fail("public text is too long")
    if "%" not in value:
        return value
    normalized = value
    decoded_once = False
    for _ in range(len(value) + 1):
        if not PERCENT_ESCAPE.search(normalized):
            if decoded_once and DECODED_MALFORMED_PERCENT.search(normalized):
                _fail("public text contains an ambiguous encoded percent escape")
            return normalized
        try:
            decoded = unquote(normalized, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            _fail("public text contains an invalid percent escape")
        if len(decoded) > MAX_PUBLIC_TEXT_LENGTH:
            _fail("normalized public text is too long")
        if decoded == normalized:
            return normalized
        normalized = decoded
        decoded_once = True
    _fail("public text exceeded the percent-normalization limit")


def _contains_disallowed_public_character(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C")
        or char in PUBLIC_TEXT_SEPARATORS
        for char in value
    )


def _check_private_values(document: dict[str, object]) -> None:
    """Enforce the closed public schema: no URLs, URIs, or filesystem paths."""
    for value in _public_values(document):
        if len(value) > MAX_PUBLIC_TEXT_LENGTH:
            _fail("public text is too long")
        if _contains_disallowed_public_character(value):
            _fail("public text contains a control character")
        normalized = _normalize_percent_encoding(value)
        if _contains_disallowed_public_character(normalized):
            _fail("normalized public text contains a control character")
        if URL_OR_URI.search(normalized):
            _fail("URL or URI value is not allowed in promoted results")
        if PRIVATE_PATH.search(normalized):
            _fail("private-looking value in promoted result")


def _required_keys(value: dict[str, object], required: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        _fail(f"missing {label} field: {missing[0]}")


def _digest(value: object, label: str) -> str:
    digest = _string(value, label)
    if not SHA256.fullmatch(digest):
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _trusted_digest(value: object) -> str:
    digest = _string(value, "trusted benchmark input SHA-256")
    if not TRUSTED_SHA256.fullmatch(digest):
        _fail("trusted benchmark input SHA-256 must be a 64-hex digest")
    return digest.lower()


def _fixture_hash_name(value: object) -> str:
    name = _string(value, "fixture hash name")
    if name == "manifest":
        return name
    prefix, separator, relative = name.partition(":")
    if prefix not in {"input", "truth"} or not separator:
        _fail("fixture hash name is not public")
    parts = relative.split("/")
    if not parts or any(
        part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
        for part in parts
    ):
        _fail("fixture hash name is not a safe relative identifier")
    return name


def _validate_scenario(value: object, label: str) -> dict[str, object]:
    scenario = _mapping(value, label)
    _only_keys(scenario, SCENARIO_KEYS, "scenario")
    _required_keys(scenario, SCENARIO_KEYS, "scenario")
    suite = _string(scenario["suite"], f"{label}.suite")
    source = _string(scenario["source"], f"{label}.source")
    document_type = _string(scenario["document_type"], f"{label}.document_type")
    method = _string(scenario["method"], f"{label}.method")
    device = _string(scenario["device"], f"{label}.device")
    language = scenario["language"]
    expected_backends = scenario["expected_backends"]
    if suite not in SCENARIO_SUITES:
        _fail("unsupported scenario suite")
    if not SOURCE.fullmatch(source):
        _fail("scenario source is not a safe relative identifier")
    if document_type not in DOCUMENT_TYPES:
        _fail("unsupported scenario document type")
    if method not in SCENARIO_METHODS:
        _fail("unsupported scenario method")
    if device not in SCENARIO_DEVICES:
        _fail("unsupported scenario device")
    if language is not None and (
        not isinstance(language, str) or language not in LANGUAGES
    ):
        _fail("scenario language is not approved")
    if not isinstance(expected_backends, list) or not expected_backends:
        _fail("scenario expected_backends must be a non-empty list")
    if (
        not all(isinstance(backend, str) and backend in BACKENDS for backend in expected_backends)
        or len(set(expected_backends)) != len(expected_backends)
    ):
        _fail("scenario expected_backends are not approved")
    expected_identifier = f"{suite}:{source}:{method}:{device}"
    if language is not None:
        expected_identifier += f":{language}"
    if scenario["id"] != expected_identifier:
        _fail(f"{label} id does not match its public fields")
    return scenario


def _validate_resources(value: object) -> None:
    resources = _mapping(value, "successful sample.resources")
    _only_keys(resources, RESOURCE_KEYS, "resource")
    _required_keys(
        resources,
        {
            "peak_process_rss_mib",
            "process_rss_status",
            "pytorch_allocator_peak_mib",
            "pytorch_allocator_peak_status",
        },
        "resource",
    )
    process_peak = _nullable_finite_nonnegative(
        resources["peak_process_rss_mib"], "peak process RSS"
    )
    process_status = resources["process_rss_status"]
    if not isinstance(process_status, str) or process_status not in PROCESS_RSS_STATUSES:
        _fail("process RSS status is not approved")
    if (process_status == "available") != (process_peak is not None):
        _fail("process RSS status does not match its measurement")
    allocator_peak = _nullable_finite_nonnegative(
        resources["pytorch_allocator_peak_mib"], "PyTorch allocator peak"
    )
    allocator_status = _string(
        resources["pytorch_allocator_peak_status"], "PyTorch allocator status"
    )
    if allocator_status not in PYTORCH_ALLOCATOR_STATUSES:
        _fail("PyTorch allocator status is not approved")
    if (allocator_status == "available") != (allocator_peak is not None):
        _fail("PyTorch allocator status does not match its measurement")


def _validate_quality(value: object) -> None:
    quality = _mapping(value, "successful language quality")
    _only_keys(quality, QUALITY_KEYS, "quality")
    _required_keys(quality, QUALITY_KEYS, "quality")
    for metric in QUALITY_KEYS:
        numeric = _finite_nonnegative(quality[metric], f"successful language quality {metric}")
        if numeric > 1:
            _fail(f"successful language quality {metric} must be at most one")


def _validate_document_shape(document: dict[str, object]) -> None:
    """Reject fields not deliberately approved for public benchmark evidence."""
    _only_keys(document, DOCUMENT_KEYS, "benchmark result")
    _required_keys(document, DOCUMENT_KEYS, "benchmark result")
    if (
        not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != SCHEMA_VERSION
    ):
        _fail("benchmark schema version does not match")
    _digest(document["configuration_fingerprint"], "configuration fingerprint")
    metadata = _mapping(document.get("metadata"), "metadata")
    _only_keys(metadata, METADATA_KEYS, "metadata")
    _required_keys(
        metadata,
        {
            "commit",
            "dirty_worktree",
            "purpose",
            "suite",
            "platform",
            "device",
            "fixture_hashes",
            "timing_policy",
            "scenario_plan",
        },
        "metadata",
    )
    commit = _string(metadata["commit"], "metadata.commit")
    if not COMMIT.fullmatch(commit):
        _fail("metadata.commit must be a lowercase 40-hex commit")
    if not isinstance(metadata["dirty_worktree"], bool):
        _fail("metadata.dirty_worktree must be a boolean")
    purpose = _string(metadata["purpose"], "metadata.purpose")
    if purpose not in {"production", "smoke"}:
        _fail("metadata.purpose is not approved")
    suite = _string(metadata["suite"], "metadata.suite")
    if suite not in {"format", "language", "all"}:
        _fail("metadata.suite is not approved")
    platform = _string(metadata["platform"], "metadata.platform")
    if not PLATFORM.fullmatch(platform):
        _fail("metadata.platform is not an approved identifier")
    device = _string(metadata["device"], "metadata.device")
    if device not in SCENARIO_DEVICES:
        _fail("metadata.device is not approved")
    if "benchmark_schema_version" in metadata:
        if (
            not isinstance(metadata["benchmark_schema_version"], int)
            or isinstance(metadata["benchmark_schema_version"], bool)
            or metadata["benchmark_schema_version"] != SCHEMA_VERSION
        ):
            _fail("metadata.benchmark_schema_version does not match")
    if "cpu_count" in metadata:
        _integer(metadata["cpu_count"], "metadata.cpu_count", minimum=1)
    if "total_memory_mib" in metadata:
        _finite_nonnegative(metadata["total_memory_mib"], "metadata.total_memory_mib")
    if "python" in metadata:
        python_version = _string(metadata["python"], "metadata.python")
        if not VERSION.fullmatch(python_version):
            _fail("metadata.python must be a version string")
    if "started_at_utc" in metadata:
        started_at_utc = _string(metadata["started_at_utc"], "metadata.started_at_utc")
        if not STARTED_AT_UTC.fullmatch(started_at_utc):
            _fail("metadata.started_at_utc must include a UTC offset")
        try:
            offset = datetime.fromisoformat(started_at_utc.replace("Z", "+00:00")).utcoffset()
        except ValueError:
            _fail("metadata.started_at_utc must be an ISO-8601 timestamp")
        if offset != timedelta(0):
            _fail("metadata.started_at_utc must use UTC")
    if "os" in metadata:
        os_values = _mapping(metadata["os"], "metadata.os")
        _only_keys(os_values, OS_KEYS, "OS metadata")
        _required_keys(os_values, OS_KEYS, "OS metadata")
        for name, value in os_values.items():
            _string(value, f"metadata.os.{name}")
    if "dependencies" in metadata:
        dependency_versions = _mapping(metadata["dependencies"], "metadata.dependencies")
        dependency_keys = (
            HISTORICAL_DEPENDENCY_KEYS
            if commit == HISTORICAL_BENCHMARK_COMMIT
            else CURRENT_DEPENDENCY_KEYS
        )
        _only_keys(
            dependency_versions,
            dependency_keys,
            "dependency metadata",
        )
        for name, version in dependency_versions.items():
            version_string = _string(version, f"metadata dependency {name}")
            if not VERSION.fullmatch(version_string):
                _fail(f"metadata dependency {name} must be a version string")
    timing_policy = metadata.get("timing_policy")
    if timing_policy is not None:
        policy = _mapping(timing_policy, "metadata.timing_policy")
        _only_keys(
            policy,
            TIMING_POLICY_KEYS,
            "timing policy",
        )
        _required_keys(policy, TIMING_POLICY_KEYS, "timing policy")
        warm_repeats = _integer(policy["warm_repeats"], "timing policy warm_repeats", minimum=1)
        max_warm_repeats = _integer(
            policy["max_warm_repeats"], "timing policy max_warm_repeats", minimum=warm_repeats
        )
        _finite_nonnegative(policy["min_sample_seconds"], "timing policy min_sample_seconds")
        variance_ratio = _finite_nonnegative(policy["variance_ratio"], "timing policy variance_ratio")
        if variance_ratio > 1:
            _fail("timing policy variance_ratio must be at most one")
    fixture_hashes = metadata.get("fixture_hashes")
    if fixture_hashes is not None:
        hashes = _mapping(fixture_hashes, "metadata.fixture_hashes")
        if not hashes:
            _fail("fixture hashes cannot be empty")
        for name, digest in hashes.items():
            _fixture_hash_name(name)
            _digest(digest, "fixture hash")
    scenario_plan = metadata.get("scenario_plan")
    if not isinstance(scenario_plan, list) or not scenario_plan:
        _fail("benchmark result has no scenario plan")
    for item in scenario_plan:
        _validate_scenario(item, "scenario plan item")
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples:
        _fail("benchmark result has no samples")
    for item in samples:
        sample = _mapping(item, "sample")
        _required_keys(sample, {"scenario", "status"}, "sample")
        scenario = _validate_scenario(sample["scenario"], "sample.scenario")
        status = sample["status"]
        if status == "success":
            _only_keys(sample, SUCCESS_SAMPLE_KEYS, "sample")
            _required_keys(
                sample,
                {
                    "scenario",
                    "status",
                    "backend",
                    "marker_verified",
                    "output_sha256",
                    "warm_samples_seconds",
                    "warm_operation_counts",
                    "warm_median_seconds",
                    "warm_range_seconds",
                    "adaptive_warm_repeats",
                    "resources",
                },
                "successful sample",
            )
            backend = _string(sample["backend"], "successful sample.backend")
            if backend not in BACKENDS:
                _fail("successful sample backend is not approved")
            if not isinstance(sample["marker_verified"], bool):
                _fail("successful sample marker_verified must be a boolean")
            _digest(sample["output_sha256"], "successful sample output_sha256")
            if "initialization_seconds" in sample:
                _nullable_finite_nonnegative(
                    sample["initialization_seconds"], "successful sample initialization"
                )
            if not isinstance(sample["adaptive_warm_repeats"], bool):
                _fail("successful sample adaptive_warm_repeats must be a boolean")
            timings = sample["warm_samples_seconds"]
            counts = sample["warm_operation_counts"]
            if not isinstance(timings, list) or not timings:
                _fail("successful sample warm timings must be a non-empty list")
            if not isinstance(counts, list) or len(counts) != len(timings):
                _fail("successful sample warm operation counts are invalid")
            for timing in timings:
                if _finite_nonnegative(timing, "successful sample warm timing") <= 0:
                    _fail("successful sample warm timing must be positive")
            for count in counts:
                _integer(count, "successful sample operation count", minimum=1)
            if _finite_nonnegative(sample["warm_median_seconds"], "successful sample warm median") <= 0:
                _fail("successful sample warm median must be positive")
            _finite_nonnegative(sample["warm_range_seconds"], "successful sample warm range")
            _validate_resources(sample["resources"])
            quality = sample.get("quality")
            if scenario["suite"] == "language":
                if quality is None:
                    _fail("successful language sample requires quality")
                _validate_quality(quality)
            elif quality is not None:
                _fail("format sample quality must be null or absent")
        elif status == "unsupported":
            _only_keys(sample, UNSUPPORTED_SAMPLE_KEYS, "sample")
            _required_keys(
                sample,
                {"scenario", "status", "error_type"},
                "unsupported sample",
            )
            if _string(sample["error_type"], "unsupported error_type") != "NoEngineFor":
                _fail("unsupported language scenario must use NoEngineFor")
            if "error_message" in sample:
                _string(sample["error_message"], "unsupported error_message")
        else:
            # This preserves the established error for rejected format failures
            # while keeping a future status-policy expansion schema-closed.
            _only_keys(sample, FAILED_SAMPLE_KEYS, "sample")


def _validate_trusted_provenance(
    documents: Sequence[dict[str, object]],
    *,
    manifest_path: Path,
    corpus: Path,
    trusted_commit: str,
) -> None:
    """Bind raw run JSON to local, clean fixtures before public promotion.

    Result JSON is resumable working state, not an authority by itself.  This
    rebuilds each device-specific scenario plan and fingerprint from the
    selected checkout and its generated fixtures, so a forged internal plan or
    fixture hash cannot become publishable merely by being self-consistent.
    """
    for unchecked_document in documents:
        document = _mapping(unchecked_document, "benchmark result")
        metadata = _mapping(document.get("metadata"), "metadata")
        commit = _string(metadata.get("commit"), "metadata.commit")
        if commit != trusted_commit:
            _fail("benchmark result commit does not match the clean promotion checkout")
        timing_policy = _mapping(metadata.get("timing_policy"), "metadata.timing_policy")
        _only_keys(timing_policy, TIMING_POLICY_KEYS, "timing policy")
        if timing_policy != PRODUCTION_TIMING_POLICY:
            _fail("benchmark timing policy does not match the production standard")
        device = _string(metadata.get("device"), "metadata.device")
        try:
            scenarios, fixture_hashes, fingerprint = benchmark_fingerprint(
                manifest_path=manifest_path,
                corpus=corpus,
                suite="all",
                device=device,
                commit=trusted_commit,
                timing_policy=timing_policy,
            )
        except (OSError, ValueError) as error:
            _fail(f"cannot rebuild trusted benchmark plan: {error}")
        expected_plan = [scenario.public_plan() for scenario in scenarios]
        if metadata.get("scenario_plan") != expected_plan:
            _fail("benchmark scenario plan does not match trusted fixtures")
        if metadata.get("fixture_hashes") != fixture_hashes:
            _fail("benchmark fixture hashes do not match trusted fixtures")
        if document.get("configuration_fingerprint") != fingerprint:
            _fail("benchmark fingerprint does not match trusted fixtures")


def _sample_row(
    *, metadata: dict[str, object], sample: object
) -> dict[str, object]:
    entry = _mapping(sample, "sample")
    scenario = _mapping(entry.get("scenario"), "sample.scenario")
    identifier = _string(scenario.get("id"), "sample.scenario.id")
    suite = _string(scenario.get("suite"), "sample.scenario.suite")
    source = _string(scenario.get("source"), "sample.scenario.source")
    document_type = _string(
        scenario.get("document_type"), "sample.scenario.document_type"
    )
    method = _string(scenario.get("method"), "sample.scenario.method")
    scenario_device = _string(scenario.get("device"), "sample.scenario.device")
    platform = _string(metadata.get("platform"), "metadata.platform")
    device = _string(metadata.get("device"), "metadata.device")
    status = _string(entry.get("status"), "sample.status")
    language = scenario.get("language")
    if language is not None and not isinstance(language, str):
        _fail("sample.scenario.language must be a string or null")
    expected_backends = scenario.get("expected_backends")
    if not isinstance(expected_backends, list) or not all(
        isinstance(item, str) for item in expected_backends
    ):
        _fail("sample.scenario.expected_backends must be a string list")
    if suite not in {"format", "language"}:
        _fail("unsupported scenario suite")
    if method not in {"default", "ocr", "docling", "docling+ocr"}:
        _fail("unsupported scenario method")
    if scenario_device not in {"cpu", "cuda", "mps", "xpu"}:
        _fail("unsupported scenario device")
    if not expected_backends:
        _fail("sample.scenario.expected_backends cannot be empty")

    uses_docling = method in {"docling", "docling+ocr"}
    if uses_docling and scenario_device != device:
        _fail("Docling scenario device does not match result device")
    if not uses_docling and device != "cpu":
        _fail("non-Docling scenario appears outside the CPU baseline")

    row: dict[str, object] = {
        "id": identifier,
        "platform": platform,
        "device": device,
        "suite": suite,
        "source": source,
        "document_type": document_type,
        "language": language or "",
        "method": method,
        "status": status,
        "backend": entry.get("backend", ""),
        "initialization_seconds": entry.get("initialization_seconds", ""),
        "warm_median_seconds": entry.get("warm_median_seconds", ""),
        "warm_range_seconds": entry.get("warm_range_seconds", ""),
        "warm_operation_counts": entry.get("warm_operation_counts", []),
        "peak_process_rss_mib": "",
        "pytorch_allocator_peak_mib": "",
        "pytorch_allocator_peak_status": "",
        "cer": "",
        "cer_ns": "",
        "cer_bag": "",
    }
    if suite == "format" and status != "success":
        _fail("format scenario is not a successful validated result")
    if suite == "language" and status not in {"success", "unsupported"}:
        _fail("language scenario must be success or structured unsupported")
    if status == "unsupported":
        if _string(entry.get("error_type"), "unsupported error_type") != "NoEngineFor":
            _fail("unsupported language scenario must use NoEngineFor")
        return row
    if status != "success":
        _fail(f"unsupported benchmark status: {status}")

    backend = _string(entry.get("backend"), "successful sample.backend")
    if backend not in expected_backends:
        _fail("successful sample backend is outside expected_backends")
    if suite == "format" and entry.get("marker_verified") is not True:
        _fail("format sample marker was not verified")
    samples = entry.get("warm_samples_seconds")
    counts = entry.get("warm_operation_counts")
    if not isinstance(samples, list) or not isinstance(counts, list) or len(samples) != len(counts):
        _fail("successful sample warm timing/count arrays are invalid")
    timing_policy = _mapping(metadata.get("timing_policy"), "metadata.timing_policy")
    warm_repeats = timing_policy.get("warm_repeats")
    max_warm_repeats = timing_policy.get("max_warm_repeats")
    if (
        not isinstance(warm_repeats, int)
        or isinstance(warm_repeats, bool)
        or warm_repeats < 1
        or not isinstance(max_warm_repeats, int)
        or isinstance(max_warm_repeats, bool)
        or max_warm_repeats < warm_repeats
    ):
        _fail("metadata timing policy has invalid repeat counts")
    if len(samples) not in {warm_repeats, max_warm_repeats}:
        _fail("successful sample has an unexpected warm repeat count")
    values = [
        _finite_positive(value, "successful sample warm timing") for value in samples
    ]
    if not all(isinstance(count, int) and count > 0 for count in counts):
        _fail("successful sample operation counts must be positive integers")
    median = _finite_positive(
        entry.get("warm_median_seconds"), "successful sample warm median"
    )
    if not math.isclose(median, statistics.median(values), rel_tol=1e-9, abs_tol=0):
        _fail("successful sample warm median disagrees with its samples")
    expected_range = max(values) - min(values)
    reported_range = entry.get("warm_range_seconds")
    if (
        not isinstance(reported_range, (int, float))
        or isinstance(reported_range, bool)
        or not math.isfinite(float(reported_range))
        or float(reported_range) < 0
        or not math.isclose(
            float(reported_range), expected_range, rel_tol=1e-9, abs_tol=1e-12
        )
    ):
        _fail("successful sample warm range disagrees with its samples")
    resources = _mapping(entry.get("resources"), "successful sample.resources")
    row["peak_process_rss_mib"] = resources.get("peak_process_rss_mib", "")
    row["pytorch_allocator_peak_mib"] = resources.get(
        "pytorch_allocator_peak_mib", ""
    )
    row["pytorch_allocator_peak_status"] = resources.get(
        "pytorch_allocator_peak_status", ""
    )
    if suite == "language":
        quality = _mapping(entry.get("quality"), "successful language quality")
        for metric in ("cer", "cer_ns", "cer_bag"):
            value = quality.get(metric)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                _fail(f"successful language quality {metric} must be finite")
            row[metric] = float(value)
    return row


def validate_result_documents(
    documents: Sequence[dict[str, object]],
    *,
    required_runs: Sequence[tuple[str, str]] = (),
) -> list[dict[str, object]]:
    """Return flattened publishable rows or reject incomplete/private output."""
    if not documents:
        _fail("no benchmark result documents")
    runs: set[tuple[str, str]] = set()
    commits: set[str] = set()
    fixture_sets: set[str] = set()
    rows: list[dict[str, object]] = []
    for unchecked_document in documents:
        document = _mapping(unchecked_document, "benchmark result")
        _check_private_values(document)
        _validate_document_shape(document)
        if document.get("schema_version") != SCHEMA_VERSION:
            _fail("benchmark schema version does not match")
        _string(document.get("configuration_fingerprint"), "configuration_fingerprint")
        metadata = _mapping(document.get("metadata"), "metadata")
        commit = _string(metadata.get("commit"), "metadata.commit")
        if metadata.get("dirty_worktree") is not False:
            _fail("public benchmark result was collected from a dirty worktree")
        purpose = _string(metadata.get("purpose"), "metadata.purpose")
        if purpose != "production":
            _fail("smoke benchmark results cannot be promoted")
        if _string(metadata.get("suite"), "metadata.suite") != "all":
            _fail("public benchmark result must use the all suite")
        platform = _string(metadata.get("platform"), "metadata.platform")
        device = _string(metadata.get("device"), "metadata.device")
        run = (platform, device)
        if run in runs:
            _fail("duplicate platform/device result")
        runs.add(run)
        commits.add(commit)
        fixture_hashes = _mapping(metadata.get("fixture_hashes"), "metadata.fixture_hashes")
        _string(fixture_hashes.get("manifest"), "manifest fixture hash")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in fixture_hashes.items()
        ):
            _fail("fixture hashes must map public names to digest strings")
        fixture_sets.add(json.dumps(fixture_hashes, sort_keys=True, separators=(",", ":")))
        samples = document.get("samples")
        if not isinstance(samples, list) or not samples:
            _fail("benchmark result has no samples")
        scenario_plan = metadata.get("scenario_plan")
        if not isinstance(scenario_plan, list) or not scenario_plan:
            _fail("benchmark result has no scenario plan")
        planned: dict[str, dict[str, object]] = {}
        for item in scenario_plan:
            plan = _mapping(item, "scenario plan item")
            identifier = _string(plan.get("id"), "scenario plan item id")
            if identifier in planned:
                _fail("duplicate scenario within a scenario plan")
            planned[identifier] = plan
        identifiers: set[str] = set()
        for sample in samples:
            sample_object = _mapping(sample, "sample")
            sample_scenario = _mapping(sample_object.get("scenario"), "sample.scenario")
            sample_identifier = _string(sample_scenario.get("id"), "sample.scenario.id")
            if sample_scenario != planned.get(sample_identifier):
                _fail("sample scenario does not match the recorded scenario plan")
            row = _sample_row(metadata=metadata, sample=sample)
            if row["id"] in identifiers:
                _fail("duplicate scenario within a platform/device result")
            identifiers.add(str(row["id"]))
            rows.append(row)
        if identifiers != set(planned):
            _fail("completed samples do not cover the recorded scenario plan")
    if len(commits) != 1:
        _fail("benchmark result commits differ")
    if len(fixture_sets) != 1:
        _fail("benchmark fixture hashes differ")
    missing = set(required_runs) - runs
    if missing:
        rendered = ", ".join(f"{platform}/{device}" for platform, device in sorted(missing))
        _fail(f"missing required platform/device runs: {rendered}")
    return sorted(
        rows,
        key=lambda row: (
            str(row["platform"]),
            str(row["device"]),
            str(row["suite"]),
            str(row["source"]),
            str(row["method"]),
        ),
    )


CSV_FIELDS = (
    "platform",
    "device",
    "suite",
    "source",
    "document_type",
    "language",
    "method",
    "backend",
    "status",
    "initialization_seconds",
    "warm_median_seconds",
    "warm_range_seconds",
    "warm_operation_counts",
    "peak_process_rss_mib",
    "pytorch_allocator_peak_mib",
    "pytorch_allocator_peak_status",
    "cer",
    "cer_ns",
    "cer_bag",
)


def _csv_literal(value: object) -> object:
    """Keep text cells literal when a result is opened in spreadsheet software."""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r")
    if value.startswith(("\t", "\r")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_csv(rows: Sequence[dict[str, object]]) -> str:
    """Render sorted, flat, per-scenario evidence without averaged claims."""
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        rendered = dict(row)
        rendered["warm_operation_counts"] = ";".join(
            str(value) for value in row["warm_operation_counts"]
        )
        writer.writerow(
            {
                field: _csv_literal(rendered.get(field, ""))
                for field in CSV_FIELDS
            }
        )
    return buffer.getvalue()


def _markdown_value(value: object) -> str:
    if value == "":
        return "—"
    if isinstance(value, float):
        text = f"{value:.6g}"
    else:
        text = str(value)
    # Renderers may also be called directly, so retain table safety even when
    # their rows did not come through the promotion validator.
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("!", "\\!")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_markdown(rows: Sequence[dict[str, object]], *, commit: str) -> str:
    """Render a compact comparison table while preserving every dimension."""
    fields = (
        ("platform", "Platform"),
        ("device", "Device"),
        ("suite", "Suite"),
        ("source", "Source"),
        ("document_type", "Document type"),
        ("language", "Language"),
        ("method", "Method"),
        ("backend", "Backend"),
        ("status", "Status"),
        ("initialization_seconds", "Init s"),
        ("warm_median_seconds", "Warm median s"),
        ("warm_range_seconds", "Warm range s"),
        ("cer_ns", "CER no-space"),
    )
    lines = [
        "# Production benchmark results",
        "",
        f"Commit: `{commit}`",
        "",
        "| " + " | ".join(label for _key, label in fields) + " |",
        "| " + " | ".join("---" for _key, _label in fields) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(_markdown_value(row[key]) for key, _label in fields)
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        _fail("platform/device label cannot produce a safe filename")
    return slug


def _sanitized_document(document: dict[str, object]) -> dict[str, object]:
    """Copy only the schema fields intended for a public evidence artifact.

    Validation has already rejected unknown fields before this helper runs.
    Reconstructing the document nevertheless makes the publication boundary
    explicit, and deliberately omits free-form failure diagnostics from an
    otherwise publishable structured ``unsupported`` result.
    """
    metadata = _mapping(document["metadata"], "metadata")
    public_samples: list[dict[str, object]] = []
    samples = document["samples"]
    assert isinstance(samples, list)
    for item in samples:
        sample = _mapping(item, "sample")
        if sample["status"] == "success":
            public_samples.append(
                {
                    key: sample[key]
                    for key in SUCCESS_SAMPLE_KEYS
                    if key in sample
                }
            )
        else:
            public_samples.append(
                {
                    key: sample[key]
                    for key in ("scenario", "status", "error_type")
                    if key in sample
                }
            )
    return {
        "schema_version": document["schema_version"],
        "configuration_fingerprint": document["configuration_fingerprint"],
        "metadata": {
            key: metadata[key] for key in METADATA_KEYS if key in metadata
        },
        "samples": public_samples,
    }


def _load_result_document(
    path: Path, *, expected_sha256: str
) -> dict[str, object]:
    try:
        parsed = load_bounded_strict_json(
            path,
            max_bytes=MAX_RAW_RESULT_BYTES,
            expected_sha256=expected_sha256,
        )
    except ValueError as error:
        _fail(f"cannot parse benchmark result {path.name}: {error}")
    return _mapping(parsed, "benchmark result")


def promote_results(
    input_paths: Sequence[Path],
    output_directory: Path,
    *,
    input_sha256: Sequence[str],
    required_runs: Sequence[tuple[str, str]],
    format_manifest: Path | None = None,
    corpus: Path | None = None,
    trusted_commit: str | None = None,
) -> list[dict[str, object]]:
    """Copy validated public JSON and deterministic reports into a new folder.

    Every raw result is bound to an independently recorded digest.  The
    command-line entry point also supplies the trusted fixture inputs and clean
    checkout commit.  The optional provenance parameters keep this helper
    usable by schema-focused tests while preserving result-byte integrity.
    """
    if output_directory.exists():
        _fail(f"promotion output already exists: {output_directory.name}")
    supplied_trust_inputs = (format_manifest, corpus, trusted_commit)
    if any(value is not None for value in supplied_trust_inputs) and not all(
        value is not None for value in supplied_trust_inputs
    ):
        _fail("trusted promotion requires manifest, corpus, and clean commit")
    if len(input_paths) != len(input_sha256):
        _fail("every benchmark input requires one trusted SHA-256 digest")
    trusted_digests = [_trusted_digest(value) for value in input_sha256]
    documents = [
        _load_result_document(path, expected_sha256=digest)
        for path, digest in zip(input_paths, trusted_digests, strict=True)
    ]
    rows = validate_result_documents(documents, required_runs=required_runs)
    if format_manifest is not None and corpus is not None and trusted_commit is not None:
        _validate_trusted_provenance(
            documents,
            manifest_path=format_manifest,
            corpus=corpus,
            trusted_commit=trusted_commit,
        )
    public_documents = [_sanitized_document(document) for document in documents]
    names = []
    for document in public_documents:
        metadata = _mapping(document["metadata"], "metadata")
        names.append(
            f"{_slug(_string(metadata['platform'], 'metadata.platform'))}-"
            f"{_slug(_string(metadata['device'], 'metadata.device'))}.json"
        )
    if len(names) != len({name.casefold() for name in names}):
        _fail("promotion filename collision")
    output_directory.mkdir(parents=True)
    for document, name in zip(public_documents, names, strict=True):
        (output_directory / name).write_text(
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
    commit = _string(
        _mapping(public_documents[0]["metadata"], "metadata")["commit"],
        "metadata.commit",
    )
    (output_directory / "summary.csv").write_text(render_csv(rows), encoding="utf-8")
    (output_directory / "README.md").write_text(
        render_markdown(rows, commit=commit), encoding="utf-8"
    )
    return rows


def _required_run(value: str) -> tuple[str, str]:
    platform, separator, device = value.partition(":")
    if not separator or not platform or not device:
        raise argparse.ArgumentTypeError("required run must be PLATFORM:DEVICE")
    return platform, device


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and promote public d2md benchmark evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    promote = commands.add_parser("promote", help="validate JSON and create reports")
    promote.add_argument(
        "--input",
        nargs=2,
        action="append",
        required=True,
        metavar=("PATH", "SHA256"),
        help="raw result and its independently recorded trusted SHA-256",
    )
    promote.add_argument("--output", type=Path, required=True)
    promote.add_argument(
        "--format-manifest",
        type=Path,
        default=Path("examples/generated/manifest.json"),
        help="trusted generated format fixture manifest",
    )
    promote.add_argument(
        "--corpus",
        type=Path,
        default=Path("corpus"),
        help="trusted generated OCR corpus",
    )
    promote.add_argument(
        "--require", type=_required_run, action="append", required=True,
        help="required PLATFORM:DEVICE run (repeat for all hardware runs)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Promote validated raw JSON without exposing local runtime details."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    if args.command != "promote":  # pragma: no cover - argparse keeps this closed
        parser.error(f"unknown command: {args.command}")
    input_paths = tuple(Path(path) for path, _sha256 in args.input)
    input_sha256 = tuple(digest for _path, digest in args.input)
    try:
        rows = promote_results(
            input_paths,
            args.output,
            input_sha256=input_sha256,
            required_runs=tuple(args.require),
            format_manifest=args.format_manifest,
            corpus=args.corpus,
            trusted_commit=current_clean_commit(),
        )
    except (FileNotFoundError, RuntimeError, ValidationError, ValueError) as error:
        parser.error(display_text(error))
    print(f"promoted {len(rows)} benchmark rows to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI itself
    raise SystemExit(main())
