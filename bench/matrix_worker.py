"""Run one production-route benchmark scenario in an isolated process.

The worker deliberately imports :mod:`d2md` only when a real conversion is
needed.  That keeps the timing, validation, and result-schema rules unit
testable without loading a model, selecting an accelerator, or requiring the
optional OCR dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import statistics
import time
import unicodedata
from collections.abc import Callable
from typing import Protocol

from bench.matrix import Scenario


# This includes JSON quotes and escaping, leaving at least 64 KiB for the
# maximum 100-document CPU plan under the 1 MiB raw-result limit.
MAX_ERROR_MESSAGE_JSON_BYTES = 768


class ConversionResult(Protocol):
    """The small part of ``d2md.convert.Result`` the benchmark consumes."""

    markdown: str
    backend: str


Convert = Callable[..., ConversionResult]
Clock = Callable[[], int]
CacheKeys = Callable[[], frozenset[object]]


@dataclass(frozen=True)
class TimingPolicy:
    """Warm-sample policy shared by every hardware run."""

    warm_repeats: int = 3
    max_warm_repeats: int = 7
    min_sample_seconds: float = 0.25
    variance_ratio: float = 0.10

    def __post_init__(self) -> None:
        if self.warm_repeats < 1:
            raise ValueError("warm_repeats must be at least one")
        if self.max_warm_repeats < self.warm_repeats:
            raise ValueError("max_warm_repeats must be at least warm_repeats")
        if self.min_sample_seconds < 0:
            raise ValueError("min_sample_seconds cannot be negative")
        if self.variance_ratio < 0:
            raise ValueError("variance_ratio cannot be negative")


class ResourceSampler(Protocol):
    """Minimal injectable resource interface for a benchmark conversion."""

    def start(self) -> None: ...

    def observe(self) -> None: ...

    def result(self) -> dict[str, object]: ...


class PeakResourceSampler:
    """Best-effort process and PyTorch allocator peak observations.

    Process RSS requires the benchmark extra.  PyTorch exposes an allocator
    peak for CUDA and XPU, but not a comparable MPS peak API, so unavailable
    values remain explicit ``null`` instead of being guessed from board VRAM.
    """

    def __init__(self, device: str) -> None:
        self.device = device
        self._process = None
        self._torch = None
        self._peak_process_rss_mib: float | None = None
        self._peak_allocator_mib: float | None = None
        self._allocator_status = "not-started"
        try:
            import psutil
        except ImportError:
            self._process_status = "psutil-unavailable"
        else:
            self._process = psutil.Process()
            self._process_status = "available"

    def start(self) -> None:
        self._peak_process_rss_mib = None
        self._peak_allocator_mib = None
        if self.device == "cpu":
            self._allocator_status = "not-applicable-for-cpu"
            return
        try:
            import torch
        except ImportError:
            self._allocator_status = "pytorch-unavailable"
            return

        self._torch = torch
        if self.device == "cuda":
            api = getattr(torch, "cuda", None)
            available = bool(api and api.is_available())
        elif self.device == "xpu":
            api = getattr(torch, "xpu", None)
            available = bool(api and api.is_available())
        elif self.device == "mps":
            self._allocator_status = "mps-has-no-peak-allocator-api"
            return
        else:
            self._allocator_status = "unsupported-device"
            return

        if not available or api is None:
            self._allocator_status = "device-unavailable"
            return
        reset = getattr(api, "reset_peak_memory_stats", None)
        peak = getattr(api, "max_memory_allocated", None)
        if not callable(reset) or not callable(peak):
            self._allocator_status = "peak-allocator-api-unavailable"
            return
        try:
            reset()
        except Exception:
            self._allocator_status = "unable-to-reset-allocator-peak"
            return
        self._allocator_status = "available"

    def observe(self) -> None:
        if self._process is not None:
            try:
                rss_mib = self._process.memory_info().rss / (1024 * 1024)
            except Exception:
                self._process_status = "rss-unavailable"
            else:
                self._peak_process_rss_mib = max(
                    self._peak_process_rss_mib or 0, rss_mib
                )

        if self._allocator_status != "available" or self._torch is None:
            return
        api = getattr(self._torch, self.device)
        try:
            allocated_mib = api.max_memory_allocated() / (1024 * 1024)
        except Exception:
            self._allocator_status = "allocator-peak-unavailable"
        else:
            self._peak_allocator_mib = max(
                self._peak_allocator_mib or 0, allocated_mib
            )

    def result(self) -> dict[str, object]:
        return {
            "peak_process_rss_mib": self._peak_process_rss_mib,
            "process_rss_status": self._process_status,
            "pytorch_allocator_peak_mib": self._peak_allocator_mib,
            "pytorch_allocator_peak_status": self._allocator_status,
        }


def create_resource_sampler(device: str) -> ResourceSampler:
    """Construct the optional sampler only inside a benchmark worker."""
    return PeakResourceSampler(device)


def _production_converter() -> tuple[Convert, CacheKeys]:
    """Import production conversion and its exact converter-cache view lazily."""
    import importlib

    module = importlib.import_module("d2md.convert")

    def cache_keys() -> frozenset[object]:
        return frozenset(module._converters)

    return module.convert, cache_keys


def _normalise(text: str) -> str:
    return " ".join(text.casefold().split())


def _marker_verified(markdown: str, marker: str | None) -> bool:
    if marker is None:
        return True
    return _normalise(marker) in _normalise(markdown)


def _output_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _safe_error_message(error: BaseException, scenario: Scenario) -> str:
    """Keep useful diagnostics without leaking the local fixture location."""
    message = str(error).replace(str(scenario.input_path), scenario.source)
    if scenario.truth_path is not None:
        message = message.replace(str(scenario.truth_path), scenario.source)
    # Newlines make JSON reports awkward and exception messages should never
    # carry a document excerpt into a public artifact.
    message = re.sub(r"\s+", " ", message).strip()
    message = "".join(
        "�"
        if unicodedata.category(character).startswith("C")
        or not character.isprintable()
        else character
        for character in message
    )
    serialized_size = len(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    if serialized_size > MAX_ERROR_MESSAGE_JSON_BYTES:
        suffix = "…"
        serialized_size = len(
            json.dumps(suffix, ensure_ascii=False).encode("utf-8")
        )
        prefix: list[str] = []
        for character in message:
            character_size = (
                len(json.dumps(character, ensure_ascii=False).encode("utf-8")) - 2
            )
            if serialized_size + character_size > MAX_ERROR_MESSAGE_JSON_BYTES:
                break
            prefix.append(character)
            serialized_size += character_size
        return "".join(prefix) + suffix
    return message


def _unsupported_error(error: BaseException) -> BaseException | None:
    """Find a production OCR capability error, including its exception cause.

    The public conversion API can wrap an exception while replacing a private
    snapshot pathname.  The benchmark must preserve the capability outcome in
    that case, rather than calling a deliberately unsupported script a route
    failure.
    """
    try:
        from d2md.ocr import NoEngineFor
    except ImportError:
        def is_no_engine(candidate: BaseException) -> bool:
            return candidate.__class__.__name__ == "NoEngineFor"
    else:
        def is_no_engine(candidate: BaseException) -> bool:
            return isinstance(candidate, NoEngineFor)

    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if is_no_engine(current):
            return current
        visited.add(id(current))
        cause = current.__cause__
        current = cause if isinstance(cause, BaseException) else None
    return None


def _error_record(error: BaseException, scenario: Scenario) -> dict[str, object]:
    unsupported = _unsupported_error(error)
    if unsupported is None:
        status = "error"
        error_type = type(error).__name__
        message_error = error
    else:
        status = "unsupported"
        error_type = type(unsupported).__name__
        message_error = unsupported
    return {
        "scenario": scenario.public_plan(),
        "status": status,
        "error_type": error_type,
        "error_message": _safe_error_message(message_error, scenario),
    }


def _validate_result(
    result: ConversionResult, scenario: Scenario
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """Return a private-normalised result or a public validation failure."""
    markdown = result.markdown
    backend = result.backend
    marker_verified = _marker_verified(markdown, scenario.marker)
    backend_verified = backend in scenario.expected_backends
    if not backend_verified or not marker_verified:
        reasons: list[str] = []
        if not backend_verified:
            reasons.append("unexpected backend")
        if not marker_verified:
            reasons.append("expected marker was not found")
        return None, {
            "scenario": scenario.public_plan(),
            "status": "validation_error",
            "backend": backend,
            "expected_backends": list(scenario.expected_backends),
            "marker_verified": marker_verified,
            "error_type": "BenchmarkValidationError",
            "error_message": "; ".join(reasons),
            "output_sha256": _output_hash(markdown),
        }
    return {
        "backend": backend,
        "marker_verified": marker_verified,
        "output_sha256": _output_hash(markdown),
    }, {}


def _quality(markdown: str, scenario: Scenario) -> dict[str, float] | None:
    """Score language output while retaining neither prediction nor truth."""
    if scenario.suite != "language":
        return None
    if scenario.truth_path is None:
        raise ValueError(f"language scenario has no truth file: {scenario.source}")
    from bench.score import cer, cer_bag

    truth = scenario.truth_path.read_text(encoding="utf-8")
    return {
        "cer": cer(markdown, truth),
        "cer_ns": cer(markdown, truth, ignore_space=True),
        "cer_bag": cer_bag(markdown, truth),
    }


def _run_sample(
    scenario: Scenario,
    convert: Convert,
    clock_ns: Clock,
    min_sample_seconds: float,
    after_operation: Callable[[], None] | None = None,
) -> tuple[ConversionResult, float, int]:
    """Time enough identical operations to make a stable sample."""
    started = clock_ns()
    operations = 0
    result: ConversionResult | None = None
    while True:
        result = convert(scenario.input_path, **scenario.convert_kwargs())
        operations += 1
        if after_operation is not None:
            after_operation()
        elapsed_seconds = (clock_ns() - started) / 1_000_000_000
        if elapsed_seconds >= min_sample_seconds:
            break
    assert result is not None
    return result, elapsed_seconds / operations, operations


def _should_expand(samples: list[float], variance_ratio: float) -> bool:
    if len(samples) < 2:
        return False
    median = statistics.median(samples)
    if median <= 0:
        return True
    return (max(samples) - min(samples)) / median > variance_ratio


def run_scenario(
    scenario: Scenario,
    *,
    convert: Convert | None = None,
    clock_ns: Clock = time.perf_counter_ns,
    policy: TimingPolicy = TimingPolicy(),
    resource_sampler: ResourceSampler | None = None,
    cache_keys: CacheKeys | None = None,
) -> dict[str, object]:
    """Validate and time one scenario without publishing document contents.

    A Docling conversion is initialization only when its exact production
    converter-cache key is new; repeated cache keys get an unreported warm-up.
    Other routes also get an unreported warm-up conversion so their
    steady-state samples have the same meaning.
    """
    if convert is None:
        active_convert, active_cache_keys = _production_converter()
    else:
        active_convert = convert
        active_cache_keys = cache_keys
    sampler = (
        create_resource_sampler(scenario.device)
        if resource_sampler is None
        else resource_sampler
    )

    try:
        sampler.start()
        initialization_seconds: float | None = None
        if scenario.uses_docling:
            before_keys = active_cache_keys() if active_cache_keys is not None else None
            initialized, first_seconds, _ = _run_sample(
                scenario,
                active_convert,
                clock_ns,
                min_sample_seconds=0,
                after_operation=sampler.observe,
            )
            validation, failure = _validate_result(initialized, scenario)
            if validation is None:
                return failure
            if (
                before_keys is None
                or active_cache_keys is None
                or active_cache_keys() != before_keys
            ):
                initialization_seconds = first_seconds
        else:
            warmed, _, _ = _run_sample(
                scenario,
                active_convert,
                clock_ns,
                min_sample_seconds=0,
                after_operation=sampler.observe,
            )
            validation, failure = _validate_result(warmed, scenario)
            if validation is None:
                return failure

        samples: list[float] = []
        operation_counts: list[int] = []
        last_validation: dict[str, object] | None = None
        last_markdown: str | None = None
        target = policy.warm_repeats
        while len(samples) < target:
            result, seconds, operations = _run_sample(
                scenario,
                active_convert,
                clock_ns,
                min_sample_seconds=policy.min_sample_seconds,
                after_operation=sampler.observe,
            )
            validation, failure = _validate_result(result, scenario)
            if validation is None:
                return failure
            last_validation = validation
            last_markdown = result.markdown
            samples.append(seconds)
            operation_counts.append(operations)
            if (
                len(samples) == policy.warm_repeats
                and _should_expand(samples, policy.variance_ratio)
            ):
                target = policy.max_warm_repeats

        assert last_validation is not None
        assert last_markdown is not None
        record: dict[str, object] = {
            "scenario": scenario.public_plan(),
            "status": "success",
            **last_validation,
            "warm_samples_seconds": samples,
            "warm_operation_counts": operation_counts,
            "warm_median_seconds": statistics.median(samples),
            "warm_range_seconds": max(samples) - min(samples),
            "adaptive_warm_repeats": len(samples) > policy.warm_repeats,
            "resources": sampler.result(),
        }
        if initialization_seconds is not None:
            record["initialization_seconds"] = initialization_seconds
        quality = _quality(last_markdown, scenario)
        if quality is not None:
            record["quality"] = quality
        return record
    except Exception as error:
        return _error_record(error, scenario)
