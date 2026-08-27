import hashlib
import json
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import pytest

import bench.matrix as benchmark_matrix
import bench.matrix_report as benchmark_report
import bench.matrix_worker as benchmark_worker
import bench.run as legacy_run
import bench.shipped as shipped_run
from bench.matrix import (
    LANGUAGE_SAMPLES,
    ResultStore,
    ResumeMismatch,
    Scenario,
    configuration_fingerprint,
    run_planned_benchmark,
    run_scenario_set,
    scenarios_for_device,
    plan_format_scenarios,
    plan_language_scenarios,
)
from bench.matrix_worker import TimingPolicy, run_scenario
from bench.matrix_report import (
    ValidationError,
    promote_results,
    render_csv,
    render_markdown,
    validate_result_documents,
)
from d2md.errors import ConversionError
from d2md.ocr import NoEngineFor


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_benchmark_readme_targets_current_explicit_routing():
    design = (Path(__file__).parents[1] / "bench" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "`--fast`" not in design
    assert "the lightweight default" in design
    assert "`--ocr`" in design
    assert "`--docling`" in design
    assert "`--docling --ocr`" in design


def test_benchmark_resource_sampler_is_not_a_runtime_dependency():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert "psutil>=5.9" in project["optional-dependencies"]["benchmark"]
    assert "psutil" not in "\n".join(project["dependencies"])


def test_benchmark_metadata_discovers_the_d2md_distribution(monkeypatch):
    discovered = []

    def package_version(name):
        discovered.append(name)
        return "0.1.0" if name == "d2md" else None

    monkeypatch.setattr(benchmark_matrix, "_package_version", package_version)

    metadata = benchmark_matrix._run_metadata(
        commit="a" * 40,
        platform_label="test",
        device="cpu",
        timing_policy=TimingPolicy(),
        fixture_hashes={},
    )

    assert metadata["dependencies"] == {"d2md": "0.1.0"}
    assert "doc2md" not in discovered


def _manifest(tmp_path: Path) -> Path:
    generated = tmp_path / "generated"
    generated.mkdir()
    documents = [
        ("notes.txt", ".txt", "base"),
        ("report.docx", ".docx", "base"),
        ("pdf-born-digital.pdf", ".pdf", "base"),
        ("pdf-scanned.pdf", ".pdf", "ocr"),
        ("image-png.png", ".png", "ocr"),
    ]
    for name, _extension, _capability in documents:
        (generated / name).write_bytes(b"fixture")
    (generated / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "file": name,
                        "extension": extension,
                        "expected_text": "MARKER",
                        "capability": capability,
                    }
                    for name, extension, capability in documents
                ]
            }
        ),
        encoding="utf-8",
    )
    return generated / "manifest.json"


def test_format_plan_uses_explicit_modes_and_limits_devices_to_docling(tmp_path):
    scenarios = plan_format_scenarios(
        _manifest(tmp_path),
        docling_devices=("cpu", "cuda"),
    )

    by_source = {}
    for scenario in scenarios:
        by_source.setdefault(scenario.source, []).append(scenario)

    assert [(item.method, item.device, item.expected_backends) for item in by_source["notes.txt"]] == [
        ("default", "cpu", ("plain",)),
    ]
    assert [(item.method, item.device, item.expected_backends) for item in by_source["report.docx"]] == [
        ("default", "cpu", ("markitdown",)),
    ]
    assert [(item.method, item.device, item.expected_backends) for item in by_source["pdf-born-digital.pdf"]] == [
        ("default", "cpu", ("pypdfium2",)),
        ("docling", "cpu", ("docling",)),
        ("docling", "cuda", ("docling",)),
    ]
    assert [(item.method, item.device, item.expected_backends) for item in by_source["pdf-scanned.pdf"]] == [
        ("ocr", "cpu", ("ocrmac", "rapidocr")),
        ("docling+ocr", "cpu", ("docling+ocr",)),
        ("docling+ocr", "cuda", ("docling+ocr",)),
    ]
    assert [(item.method, item.device, item.expected_backends) for item in by_source["image-png.png"]] == [
        ("ocr", "cpu", ("ocrmac", "rapidocr")),
        ("docling+ocr", "cpu", ("docling+ocr",)),
        ("docling+ocr", "cuda", ("docling+ocr",)),
    ]
    assert all(item.method != "fast" for item in scenarios)
    assert all(
        item.convert_kwargs()["device"] == "auto"
        for item in scenarios
        if item.method in {"default", "ocr"}
    )


def test_format_plan_rejects_manifest_entries_outside_its_fixture_directory(tmp_path):
    manifest = _manifest(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not a fixture", encoding="utf-8")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["documents"][0]["file"] = "../outside.txt"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="fixture path"):
        plan_format_scenarios(manifest, docling_devices=("cpu",))


def test_format_plan_rejects_malformed_manifest_entries_before_sorting(tmp_path):
    manifest = _manifest(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["documents"].append("not-a-document")
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="must be an object"):
        plan_format_scenarios(manifest, docling_devices=("cpu",))


def test_format_plan_rejects_oversized_manifest_before_json_parsing(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b" " * (benchmark_matrix.MAX_FORMAT_MANIFEST_BYTES + 1))
    parser_called = False

    def parse_json(_text, **_kwargs):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input reached the JSON parser")

    monkeypatch.setattr(benchmark_matrix.json, "loads", parse_json)

    with pytest.raises(ValueError, match="exceeds"):
        plan_format_scenarios(manifest, docling_devices=("cpu",))

    assert not parser_called


def test_format_plan_rejects_too_many_documents_before_entry_validation(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"documents": ["not-a-document"] * 101}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="more than 100 documents"):
        plan_format_scenarios(manifest, docling_devices=("cpu",))


def test_format_plan_rejects_too_many_documents_before_sorting_or_traversal(
    tmp_path, monkeypatch
):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "file": f"fixture-{index}.txt",
                        "extension": ".txt",
                        "expected_text": "MARKER",
                    }
                    for index in range(101)
                ]
            }
        ),
        encoding="utf-8",
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("oversized manifest reached sorting or fixture traversal")

    monkeypatch.setattr(benchmark_matrix, "sorted", fail, raising=False)
    monkeypatch.setattr(benchmark_matrix, "_fixture_file", fail)

    with pytest.raises(ValueError, match="more than 100 documents"):
        plan_format_scenarios(manifest, docling_devices=("cpu",))


def test_maximum_format_manifest_all_error_plan_writes_a_resumable_raw_result(
    tmp_path,
    monkeypatch,
):
    generated = tmp_path / "generated"
    generated.mkdir()
    fixture = generated / "fixture.pdf"
    fixture.write_bytes(b"fixture")
    documents = []
    for index in range(benchmark_matrix.MAX_FORMAT_MANIFEST_DOCUMENTS):
        name = f"{index:03d}-" + "界" * 246 + ".pdf"
        documents.append(
            {
                "file": name,
                "extension": ".pdf",
                "expected_text": "MARKER",
                "capability": "ocr",
            }
        )
    manifest = generated / "manifest.json"
    manifest.write_text(
        json.dumps({"documents": documents}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        benchmark_matrix,
        "_fixture_file",
        lambda _root, _relative, **_kwargs: fixture,
    )
    scenarios = scenarios_for_device(
        plan_format_scenarios(manifest, docling_devices=("cpu",)),
        device="cpu",
    )
    metadata = {
        "commit": "a" * 40,
        "dirty_worktree": False,
        "purpose": "production",
        "suite": "format",
        "platform": "windows-rtx3090ti",
        "device": "cpu",
        "os": {
            "system": "Windows",
            "release": "11",
            "version": "10.0.22631 AMD64",
            "architecture": "AMD64",
        },
        "cpu_count": 128,
        "total_memory_mib": 1_048_576.0,
        "python": "3.13.7",
        "dependencies": {
            "d2md": "0.1.0",
            "docling": "2.70.0",
            "torch": "2.9.0+cu130",
            "rapidocr": "3.4.1",
            "ocrmac": "1.0.0",
            "psutil": "7.1.0",
        },
        "benchmark_schema_version": benchmark_matrix.SCHEMA_VERSION,
        "timing_policy": {
            "warm_repeats": 3,
            "max_warm_repeats": 7,
            "min_sample_seconds": 0.25,
            "variance_ratio": 0.10,
        },
        "fixture_hashes": {
            "manifest": "d" * 64,
            **{f"input:{document['file']}": "d" * 64 for document in documents},
        },
        "scenario_plan": [scenario.public_plan() for scenario in scenarios],
        "started_at_utc": "2026-08-27T12:34:56.123456+00:00",
    }
    sampler = SimpleNamespace(
        start=lambda: None,
        observe=lambda: None,
        result=lambda: {},
    )

    def fail_with_pathological_diagnostic(_path, **_kwargs):
        raise RuntimeError(
            "xx" + "\x00\\🙂" * benchmark_matrix.MAX_RAW_RESULT_BYTES
        )

    bounded_error = run_scenario(
        scenarios[0],
        convert=fail_with_pathological_diagnostic,
        resource_sampler=sampler,
    )["error_message"]
    samples = [
        {
            "scenario": scenario.public_plan(),
            "status": "error",
            "error_type": "RuntimeError",
            "error_message": bounded_error,
        }
        for scenario in scenarios
    ]
    output = tmp_path / "raw.json"
    store = ResultStore.create(output, fingerprint="same", metadata=metadata)
    store.document["samples"] = samples[:-1]
    store.append_sample(samples[-1])

    reopened = ResultStore.open(output, fingerprint="same", resume=True)

    assert len(scenarios) == 2 * benchmark_matrix.MAX_FORMAT_MANIFEST_DOCUMENTS
    assert all(len(document["file"]) == 254 for document in documents)
    assert len(
        json.dumps(bounded_error, ensure_ascii=False).encode("utf-8")
    ) == benchmark_worker.MAX_ERROR_MESSAGE_JSON_BYTES
    assert output.stat().st_size <= (
        benchmark_matrix.MAX_RAW_RESULT_BYTES - 64 * 1024
    )
    assert reopened.document == store.document
    assert benchmark_report._load_result_document(
        output, expected_sha256=_sha256(output)
    ) == store.document


def test_language_plan_keeps_ten_representatives_and_explicit_script_hints(tmp_path):
    corpus = tmp_path / "corpus"
    pdf = corpus / "pdf"
    truth = corpus / "truth"
    pdf.mkdir(parents=True)
    truth.mkdir()
    for code, _script, _label in LANGUAGE_SAMPLES:
        (pdf / f"{code}-clean.pdf").write_bytes(b"fixture")
        (truth / f"{code}-clean.txt").write_text("ground truth\n", encoding="utf-8")

    scenarios = plan_language_scenarios(
        corpus,
        docling_devices=("cpu", "mps"),
    )

    assert len(LANGUAGE_SAMPLES) == 10
    assert len(scenarios) == 30
    assert {item.language for item in scenarios} == {
        script for _code, script, _label in LANGUAGE_SAMPLES
    }
    for code, script, _label in LANGUAGE_SAMPLES:
        rows = [item for item in scenarios if item.source == f"pdf/{code}-clean.pdf"]
        assert [(item.method, item.device, item.language) for item in rows] == [
            ("ocr", "cpu", script),
            ("docling+ocr", "cpu", script),
            ("docling+ocr", "mps", script),
        ]
        assert rows[0].truth_path == truth / f"{code}-clean.txt"


def test_result_store_writes_each_completed_sample_atomically(tmp_path):
    path = tmp_path / "results.json"
    store = ResultStore.create(
        path,
        fingerprint="same-config",
        metadata={"commit": "abc123", "platform": "test"},
    )

    store.append_sample({"scenario_id": "format:notes:default:cpu", "status": "success"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["configuration_fingerprint"] == "same-config"
    assert saved["samples"] == [
        {"scenario_id": "format:notes:default:cpu", "status": "success"}
    ]
    assert not list(tmp_path.glob(".results.json.*"))


def test_result_store_writer_accepts_exact_limit_and_rejects_overflow_before_artifacts(
    tmp_path,
):
    document = {
        "schema_version": benchmark_matrix.SCHEMA_VERSION,
        "configuration_fingerprint": "same",
        "metadata": {"padding": ""},
        "samples": [],
    }
    serialized = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    padding = "x" * (benchmark_matrix.MAX_RAW_RESULT_BYTES - len(serialized))
    document["metadata"]["padding"] = padding
    serialized = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    exact = tmp_path / "exact.json"

    ResultStore.create(exact, fingerprint="same", metadata=document["metadata"])

    assert len(serialized) == benchmark_matrix.MAX_RAW_RESULT_BYTES
    assert exact.read_bytes() == serialized
    assert ResultStore.open(exact, fingerprint="same", resume=True).document == document

    overflow = tmp_path / "not-created" / "overflow.json"
    with pytest.raises(ValueError, match="exceeds.*byte limit"):
        ResultStore.create(
            overflow,
            fingerprint="same",
            metadata={"padding": padding + "x"},
        )

    assert not overflow.parent.exists()


def test_result_store_append_failure_preserves_file_and_memory_for_retry(tmp_path):
    path = tmp_path / "results.json"
    store = ResultStore.create(
        path,
        fingerprint="same",
        metadata={"commit": "abc123"},
    )
    previous_bytes = path.read_bytes()
    previous_document = deepcopy(store.document)

    with pytest.raises(ValueError, match="exceeds.*byte limit"):
        store.append_sample(
            {
                "scenario_id": "format:notes:default:cpu",
                "status": "error",
                "error_message": "x" * benchmark_matrix.MAX_RAW_RESULT_BYTES,
            }
        )

    assert path.read_bytes() == previous_bytes
    assert store.document == previous_document
    assert not list(tmp_path.glob(".results.json.*"))

    sample = {"scenario_id": "format:notes:default:cpu", "status": "success"}
    store.append_sample(sample)

    assert ResultStore.open(path, fingerprint="same", resume=True).document == {
        **previous_document,
        "samples": [sample],
    }


def test_result_store_append_cleans_temporary_file_after_replace_failure(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "results.json"
    store = ResultStore.create(path, fingerprint="same", metadata={})
    previous_bytes = path.read_bytes()
    previous_document = deepcopy(store.document)
    replace = benchmark_matrix.os.replace

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(benchmark_matrix.os, "replace", fail_replace)
    sample = {"scenario_id": "format:notes:default:cpu", "status": "success"}

    with pytest.raises(OSError, match="replace failed"):
        store.append_sample(sample)

    assert path.read_bytes() == previous_bytes
    assert store.document == previous_document
    assert not list(tmp_path.glob(".results.json.*"))

    monkeypatch.setattr(benchmark_matrix.os, "replace", replace)
    store.append_sample(sample)

    assert ResultStore.open(path, fingerprint="same", resume=True).document == {
        **previous_document,
        "samples": [sample],
    }


def test_result_store_detaches_mappings_and_recovers_from_mutating_serialization(
    tmp_path,
):
    class MutatingMapping(dict):
        fail_on_items = False

        def items(self):
            if self.fail_on_items:
                self["mutated_during_serialization"] = True
                raise RuntimeError("mapping serialization failed")
            return super().items()

    path = tmp_path / "results.json"
    metadata = MutatingMapping({"commit": "abc123"})
    store = ResultStore.create(path, fingerprint="same", metadata=metadata)
    accepted = MutatingMapping({"scenario_id": "first", "status": "success"})
    store.append_sample(accepted)

    metadata["external_mutation"] = True
    accepted["external_mutation"] = True
    accepted.fail_on_items = True
    second = {"scenario_id": "second", "status": "success"}
    store.append_sample(second)

    assert type(store.document["metadata"]) is dict
    assert type(store.document["samples"][0]) is dict
    assert "external_mutation" not in store.document["metadata"]
    assert "external_mutation" not in store.document["samples"][0]

    previous_bytes = path.read_bytes()
    previous_document = deepcopy(store.document)
    failing = MutatingMapping({"scenario_id": "failing", "status": "error"})
    failing.fail_on_items = True

    with pytest.raises(RuntimeError, match="mapping serialization failed"):
        store.append_sample(failing)

    assert failing["mutated_during_serialization"] is True
    assert path.read_bytes() == previous_bytes
    assert store.document == previous_document
    assert not list(tmp_path.glob(".results.json.*"))

    retry = {"scenario_id": "retry", "status": "success"}
    store.append_sample(retry)

    assert ResultStore.open(path, fingerprint="same", resume=True).document == {
        **previous_document,
        "samples": [*previous_document["samples"], retry],
    }


def test_result_store_rejects_resume_with_another_configuration(tmp_path):
    path = tmp_path / "results.json"
    ResultStore.create(path, fingerprint="first", metadata={"commit": "abc123"})

    with pytest.raises(ResumeMismatch, match="configuration fingerprint"):
        ResultStore.open(path, fingerprint="second", resume=True)


def test_result_store_preserves_missing_resume_file_error(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError):
        ResultStore.open(missing, fingerprint="same", resume=True)


def test_configuration_fingerprint_uses_public_scenario_fields_only(tmp_path):
    scenario = plan_format_scenarios(
        _manifest(tmp_path), docling_devices=("cpu",)
    )[0]
    common = {
        "commit": "abc123",
        "fixture_hashes": {"manifest": "digest"},
        "settings": {"warm_repeats": 3},
    }

    original = configuration_fingerprint(scenarios=[scenario], **common)
    relocated = configuration_fingerprint(
        scenarios=[replace(scenario, input_path=Path("/private/input/notes.txt"))],
        **common,
    )
    changed = configuration_fingerprint(
        scenarios=[replace(scenario, method="docling", device="cpu")],
        **common,
    )

    assert original == relocated
    assert original != changed


def test_device_selection_runs_direct_routes_only_on_the_cpu_baseline(tmp_path):
    scenarios = plan_format_scenarios(
        _manifest(tmp_path), docling_devices=("cpu", "cuda")
    )

    cpu = scenarios_for_device(scenarios, device="cpu")
    cuda = scenarios_for_device(scenarios, device="cuda")

    assert any(not item.uses_docling for item in cpu)
    assert all(item.uses_docling for item in cuda)
    assert all(item.device == "cpu" for item in cpu if item.uses_docling)
    assert all(item.device == "cuda" for item in cuda)
    assert {item.identifier for item in cpu}.isdisjoint(
        item.identifier for item in cuda
    )


def test_controller_resumes_only_completed_public_scenario_ids(tmp_path):
    scenarios = plan_format_scenarios(_manifest(tmp_path), docling_devices=("cpu",))
    output = tmp_path / "run.json"
    calls = []

    def run_one(scenario):
        calls.append(scenario.identifier)
        return {
            "scenario": scenario.public_plan(),
            "status": "success",
            "warm_samples_seconds": [1.0, 1.0, 1.0],
        }

    common = {
        "path": output,
        "fingerprint": "stable-configuration",
        "metadata": {"commit": "abc123", "platform": "test", "device": "cpu"},
        "scenarios": scenarios,
        "run_one": run_one,
    }
    first = run_scenario_set(resume=False, **common)
    second = run_scenario_set(resume=True, **common)

    assert len(calls) == len(scenarios)
    assert first.document == second.document
    assert [sample["scenario"]["id"] for sample in second.document["samples"]] == [
        scenario.identifier for scenario in scenarios
    ]


def test_controller_rejects_a_resumed_sample_whose_public_plan_was_tampered(tmp_path):
    scenarios = plan_format_scenarios(_manifest(tmp_path), docling_devices=("cpu",))
    output = tmp_path / "run.json"
    common = {
        "path": output,
        "fingerprint": "stable-configuration",
        "metadata": {"commit": "abc123", "platform": "test", "device": "cpu"},
        "scenarios": scenarios,
        "run_one": lambda scenario: {
            "scenario": scenario.public_plan(),
            "status": "success",
        },
    }
    run_scenario_set(resume=False, **common)
    document = json.loads(output.read_text(encoding="utf-8"))
    document["samples"][0]["scenario"]["source"] = "other.txt"
    output.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ResumeMismatch, match="scenario plan"):
        run_scenario_set(resume=True, **common)


def test_planned_run_emits_reproducible_public_metadata_without_local_paths(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    output = tmp_path / "benchmark.json"
    executed = []
    monkeypatch.setattr(benchmark_matrix, "current_commit", lambda: "abc123")

    def run_one(scenario):
        executed.append(scenario.identifier)
        return {
            "scenario": scenario.public_plan(),
            "status": "success",
            "warm_samples_seconds": [1.0, 1.0, 1.0],
        }

    store = run_planned_benchmark(
        manifest_path=manifest,
        corpus=None,
        suite="format",
        platform_label="test-os",
        device="cpu",
        output_path=output,
        commit="abc123",
        run_one=run_one,
        timing_policy=TimingPolicy(min_sample_seconds=0),
        resume=False,
    )

    metadata = store.document["metadata"]
    assert len(executed) == len(store.document["samples"])
    assert metadata["commit"] == "abc123"
    assert metadata["platform"] == "test-os"
    assert metadata["device"] == "cpu"
    assert metadata["purpose"] == "production"
    assert metadata["suite"] == "format"
    assert len(metadata["scenario_plan"]) == len(store.document["samples"])
    assert metadata["timing_policy"]["warm_repeats"] == 3
    assert metadata["fixture_hashes"]["manifest"]
    serialized = json.dumps(store.document)
    assert str(tmp_path) not in serialized


def test_planned_run_rejects_a_commit_override_that_is_not_the_measured_head(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(benchmark_matrix, "current_commit", lambda: "measured-head")

    with pytest.raises(ValueError, match="must match HEAD"):
        run_planned_benchmark(
            manifest_path=manifest,
            corpus=None,
            suite="format",
            platform_label="test-os",
            device="cpu",
            output_path=tmp_path / "benchmark.json",
            commit="claimed-other-head",
            run_one=lambda scenario: {"scenario": scenario.public_plan()},
            timing_policy=TimingPolicy(min_sample_seconds=0),
            resume=False,
        )


def test_planned_run_can_limit_a_smoke_execution_to_public_sources(
    tmp_path, monkeypatch
):
    manifest = _manifest(tmp_path)
    executed = []
    monkeypatch.setattr(benchmark_matrix, "current_commit", lambda: "abc123")

    store = run_planned_benchmark(
        manifest_path=manifest,
        corpus=None,
        suite="format",
        platform_label="test-os",
        device="cpu",
        output_path=tmp_path / "subset.json",
        commit="abc123",
        run_one=lambda scenario: (
            executed.append(scenario.source)
            or {"scenario": scenario.public_plan(), "status": "success"}
        ),
        timing_policy=TimingPolicy(min_sample_seconds=0),
        only_sources=("notes.txt",),
        resume=False,
    )

    assert executed == ["notes.txt"]
    assert [sample["scenario"]["source"] for sample in store.document["samples"]] == [
        "notes.txt"
    ]
    assert store.document["metadata"]["purpose"] == "smoke"


def test_matrix_cli_passes_explicit_machine_and_timing_configuration(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(document={"samples": [{}, {}]})

    monkeypatch.setattr(benchmark_matrix, "run_planned_benchmark", fake_run)

    status = benchmark_matrix.main(
        [
            "run",
            "--format-manifest", str(manifest),
            "--suite", "format",
            "--platform", "ubuntu-test",
            "--device", "cuda",
            "--output", str(tmp_path / "out.json"),
            "--warm-repeats", "4",
            "--max-warm-repeats", "5",
            "--min-sample-seconds", "0.5",
            "--variance-ratio", "0.2",
        ]
    )

    assert status == 0
    assert calls[0]["platform_label"] == "ubuntu-test"
    assert calls[0]["device"] == "cuda"
    assert calls[0]["commit"] is None
    assert calls[0]["timing_policy"] == TimingPolicy(
        warm_repeats=4,
        max_warm_repeats=5,
        min_sample_seconds=0.5,
        variance_ratio=0.2,
    )


def test_matrix_cli_does_not_allow_a_caller_to_claim_another_commit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        benchmark_matrix,
        "run_planned_benchmark",
        lambda **_kwargs: SimpleNamespace(document={"samples": []}),
    )

    with pytest.raises(SystemExit):
        benchmark_matrix.main(
            [
                "run",
                "--suite", "format",
                "--platform", "ubuntu-test",
                "--device", "cpu",
                "--output", str(tmp_path / "out.json"),
                "--commit", "claimed-other-head",
            ]
        )


def _public_result_document(*, platform="macos", device="cpu", status="success"):
    scenario = {
        "id": f"format:plain-utf8.txt:default:{device}",
        "suite": "format",
        "source": "plain-utf8.txt",
        "document_type": "direct-text",
        "method": "default",
        "device": device,
        "expected_backends": ["plain"],
        "language": None,
    }
    return {
        "schema_version": 1,
        "configuration_fingerprint": "f" * 64,
        "metadata": {
            "commit": "a" * 40,
            "dirty_worktree": False,
            "purpose": "production",
            "suite": "all",
            "platform": platform,
            "device": device,
            "fixture_hashes": {"manifest": "d" * 64},
            "timing_policy": {
                "warm_repeats": 3,
                "max_warm_repeats": 7,
                "min_sample_seconds": 0.25,
                "variance_ratio": 0.10,
            },
            "scenario_plan": [scenario],
        },
        "samples": [
            {
                "scenario": scenario,
                "status": status,
                "backend": "plain",
                "marker_verified": True,
                "output_sha256": "e" * 64,
                "warm_samples_seconds": [0.1, 0.1, 0.1],
                "warm_operation_counts": [3, 3, 3],
                "warm_median_seconds": 0.1,
                "warm_range_seconds": 0.0,
                "adaptive_warm_repeats": False,
                "resources": {
                    "peak_process_rss_mib": 100.0,
                    "process_rss_status": "available",
                    "pytorch_allocator_peak_mib": None,
                    "pytorch_allocator_peak_status": "not-applicable-for-cpu",
                },
            }
        ],
    }


def test_report_validates_public_rows_and_keeps_all_comparison_dimensions():
    documents = [_public_result_document()]

    rows = validate_result_documents(
        documents, required_runs=(("macos", "cpu"),)
    )
    csv = render_csv(rows)
    markdown = render_markdown(rows, commit="abc123")

    assert rows[0]["platform"] == "macos"
    assert rows[0]["document_type"] == "direct-text"
    assert rows[0]["method"] == "default"
    assert "platform,device,suite,source,document_type,language,method" in csv
    assert "| macos | cpu | format | plain-utf8.txt | direct-text |" in markdown


@pytest.mark.parametrize("prefix", ("=", "+", "-", "@", "\t", "\r"))
def test_report_csv_neutralizes_spreadsheet_formula_prefixes(prefix):
    row = {
        "platform": prefix + "SUM(A1:A2)",
        "warm_operation_counts": [],
    }

    assert "'" + prefix + "SUM(A1:A2)" in render_csv([row])


def test_report_rejects_missing_runs_invalid_format_samples_and_private_values():
    with pytest.raises(ValidationError, match="missing required"):
        validate_result_documents(
            [_public_result_document()],
            required_runs=(("macos", "cpu"), ("ubuntu", "cuda")),
        )

    with pytest.raises(ValidationError, match="format scenario"):
        validate_result_documents(
            [_public_result_document(status="validation_error")],
            required_runs=(("macos", "cpu"),),
        )

    document = _public_result_document()
    document["samples"][0]["error_message"] = "/private/fixture.pdf"
    with pytest.raises(ValidationError, match="private-looking value"):
        validate_result_documents(documents=[document])

    first = _public_result_document(platform="macos")
    second = _public_result_document(platform="ubuntu")
    first["metadata"]["fixture_hashes"]["input:plain-utf8.txt"] = "a" * 64
    second["metadata"]["fixture_hashes"]["input:plain-utf8.txt"] = "b" * 64
    with pytest.raises(ValidationError, match="fixture hashes differ"):
        validate_result_documents([first, second])

    smoke = _public_result_document()
    smoke["metadata"]["purpose"] = "smoke"
    with pytest.raises(ValidationError, match="smoke"):
        validate_result_documents([smoke])

    incomplete = _public_result_document()
    incomplete["metadata"]["scenario_plan"].append(
        {**incomplete["metadata"]["scenario_plan"][0], "id": "missing-row"}
    )
    with pytest.raises(ValidationError, match="scenario plan"):
        validate_result_documents([incomplete])

    invalid_method = _public_result_document()
    invalid_method["samples"][0]["scenario"]["method"] = "other"
    with pytest.raises(ValidationError, match="scenario method"):
        validate_result_documents([invalid_method])


def test_report_rejects_unknown_metadata_instead_of_copying_it_to_public_output(
    tmp_path,
):
    document = _public_result_document()
    document["metadata"]["diagnostic"] = "confidential document excerpt"
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown metadata field"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(_sha256(raw),),
            required_runs=(("macos", "cpu"),),
        )


def test_report_rejects_unknown_sample_fields_instead_of_copying_them_to_public_output(
    tmp_path,
):
    document = _public_result_document()
    document["samples"][0]["diagnostic"] = "confidential document excerpt"
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown sample field"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(_sha256(raw),),
            required_runs=(("macos", "cpu"),),
        )


def test_report_rejects_unknown_resource_fields_before_promotion(tmp_path):
    document = _public_result_document()
    document["samples"][0]["resources"]["diagnostic"] = "private metric"
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown resource field"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(_sha256(raw),),
            required_runs=(("macos", "cpu"),),
        )


def test_report_drops_unsupported_error_messages_from_promoted_json(tmp_path):
    document = _public_result_document()
    scenario = document["samples"][0]["scenario"]
    scenario.update(
        {
            "id": "language:pdf/th-clean.pdf:ocr:cpu:thai",
            "suite": "language",
            "source": "pdf/th-clean.pdf",
            "document_type": "scanned-pdf",
            "method": "ocr",
            "expected_backends": ["ocrmac"],
            "language": "thai",
        }
    )
    document["metadata"]["scenario_plan"] = [scenario]
    document["samples"] = [
        {
            "scenario": scenario,
            "status": "unsupported",
            "error_type": "NoEngineFor",
            "error_message": "confidential text-like diagnostic",
        }
    ]
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "promoted"

    promote_results(
        [raw],
        output,
        input_sha256=(_sha256(raw),),
        required_runs=(("macos", "cpu"),),
    )

    promoted = (output / "macos-cpu.json").read_text(encoding="utf-8")
    assert "confidential text-like diagnostic" not in promoted
    assert "error_message" not in promoted


def test_report_rebuilds_trusted_plan_before_promoting_result_json(tmp_path, monkeypatch):
    document = _public_result_document()
    document["configuration_fingerprint"] = "e" * 64
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")
    scenario = document["metadata"]["scenario_plan"][0]
    monkeypatch.setattr(
        benchmark_report,
        "benchmark_fingerprint",
        lambda **_kwargs: (
            [SimpleNamespace(public_plan=lambda: scenario)],
            {"manifest": "d" * 64},
            "trusted-fingerprint",
        ),
    )

    with pytest.raises(ValidationError, match="fingerprint does not match trusted"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(_sha256(raw),),
            required_runs=(("macos", "cpu"),),
            format_manifest=tmp_path / "manifest.json",
            corpus=tmp_path / "corpus",
            trusted_commit="a" * 40,
        )


def test_report_rejects_distinct_labels_that_would_collide_after_filename_slugging(
    tmp_path,
):
    first = _public_result_document(platform="macos arm64")
    second = _public_result_document(platform="macos-arm64")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")
    second_path.write_text(json.dumps(second), encoding="utf-8")

    with pytest.raises(ValidationError, match="filename collision"):
        promote_results(
            [first_path, second_path],
            tmp_path / "promoted",
            input_sha256=(_sha256(first_path), _sha256(second_path)),
            required_runs=(("macos arm64", "cpu"), ("macos-arm64", "cpu")),
        )


def test_report_promotion_copies_validated_json_and_deterministic_summaries(tmp_path):
    input_path = tmp_path / "raw.json"
    input_path.write_text(
        json.dumps(_public_result_document(), ensure_ascii=False), encoding="utf-8"
    )
    output = tmp_path / "promoted"

    rows = promote_results(
        [input_path],
        output,
        input_sha256=(_sha256(input_path),),
        required_runs=(("macos", "cpu"),),
    )

    assert len(rows) == 1
    assert (output / "macos-cpu.json").is_file()
    assert (output / "summary.csv").read_text(encoding="utf-8") == render_csv(rows)
    assert "# Production benchmark results" in (output / "README.md").read_text(
        encoding="utf-8"
    )


def test_report_cli_pairs_each_input_with_its_trusted_digest(tmp_path, monkeypatch):
    calls = []

    def fake_promote(paths, output, *, required_runs, **kwargs):
        calls.append((paths, output, required_runs, kwargs))
        return [{}, {}]

    monkeypatch.setattr(benchmark_report, "promote_results", fake_promote)
    monkeypatch.setattr(benchmark_report, "current_clean_commit", lambda: "abc123")

    status = benchmark_report.main(
        [
            "promote",
            "--input", str(tmp_path / "macos-cpu.json"), "a" * 64,
            "--input", str(tmp_path / "ubuntu-cuda.json"), "b" * 64,
            "--output", str(tmp_path / "published"),
            "--require", "macos:cpu",
            "--require", "ubuntu:cuda",
        ]
    )

    assert status == 0
    assert calls[0][0] == (
        tmp_path / "macos-cpu.json",
        tmp_path / "ubuntu-cuda.json",
    )
    assert calls[0][2] == (("macos", "cpu"), ("ubuntu", "cuda"))
    assert calls[0][3]["input_sha256"] == ("a" * 64, "b" * 64)
    assert calls[0][3]["trusted_commit"] == "abc123"


def test_legacy_run_rejects_an_engine_name_that_would_escape_the_corpus(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "pdf").mkdir()
    (corpus / "results-x").mkdir()

    assert legacy_run.main(["bench/run.py", str(corpus), "x/../../victim"]) == 2
    assert not (tmp_path / "victim.json").exists()


def test_shipped_run_rejects_an_engine_name_that_would_escape_the_corpus(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "pdf").mkdir()
    (corpus / "results-shipped-x").mkdir()

    assert shipped_run.main(["bench/shipped.py", str(corpus), "x/../../victim"]) == 2
    assert not (tmp_path / "victim.json").exists()


def test_worker_separates_docling_initialization_from_warm_samples(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="docling",
        device="cpu",
        expected_backends=("docling",),
        marker="EXPECTED MARKER",
    )
    calls = []
    clock_values = iter(
        [
            0,
            2_000_000_000,
            3_000_000_000,
            4_000_000_000,
            5_000_000_000,
            6_000_000_000,
            7_000_000_000,
            8_000_000_000,
        ]
    )

    def convert(path, **kwargs):
        calls.append((path, kwargs))
        return SimpleNamespace(markdown="EXPECTED MARKER\n", backend="docling")

    record = run_scenario(
        scenario,
        convert=convert,
        clock_ns=lambda: next(clock_values),
        policy=TimingPolicy(min_sample_seconds=0),
    )

    assert len(calls) == 4
    assert all(
        kwargs == {"ocr": False, "docling": True, "device": "cpu", "lang": None}
        for _path, kwargs in calls
    )
    assert record["status"] == "success"
    assert record["backend"] == "docling"
    assert record["marker_verified"] is True
    assert record["initialization_seconds"] == pytest.approx(2.0)
    assert record["warm_samples_seconds"] == pytest.approx([1.0, 1.0, 1.0])
    assert record["warm_median_seconds"] == pytest.approx(1.0)
    assert record["warm_operation_counts"] == [1, 1, 1]
    assert "EXPECTED MARKER" not in json.dumps(record)
    assert str(source) not in json.dumps(record)


def test_worker_records_docling_initialization_only_when_a_converter_cache_key_is_new(
    tmp_path,
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="docling",
        device="cpu",
        expected_backends=("docling",),
        marker="MARKER",
    )
    cache_keys = set()
    clock_values = iter(range(0, 40_000_000_000, 1_000_000_000))

    def convert(_path, **_kwargs):
        cache_keys.add(("latin", False, "cpu", False))
        return SimpleNamespace(markdown="MARKER", backend="docling")

    first = run_scenario(
        scenario,
        convert=convert,
        cache_keys=lambda: frozenset(cache_keys),
        clock_ns=lambda: next(clock_values),
        policy=TimingPolicy(min_sample_seconds=0, max_warm_repeats=3),
    )
    second = run_scenario(
        scenario,
        convert=convert,
        cache_keys=lambda: frozenset(cache_keys),
        clock_ns=lambda: next(clock_values),
        policy=TimingPolicy(min_sample_seconds=0, max_warm_repeats=3),
    )

    assert first["initialization_seconds"] == pytest.approx(1.0)
    assert "initialization_seconds" not in second


def test_worker_records_an_unsupported_script_without_a_timing(tmp_path):
    source = tmp_path / "th-clean.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="language",
        source="pdf/th-clean.pdf",
        input_path=source,
        document_type="scanned-pdf",
        method="ocr",
        device="cpu",
        expected_backends=("ocrmac", "rapidocr"),
        language="thai",
    )

    def convert(path, **_kwargs):
        raise NoEngineFor(f"no OCR engine for {path}")

    record = run_scenario(scenario, convert=convert)

    assert record == {
        "scenario": scenario.public_plan(),
        "status": "unsupported",
        "error_type": "NoEngineFor",
        "error_message": "no OCR engine for pdf/th-clean.pdf",
    }
    assert str(source) not in json.dumps(record)


def test_worker_bounds_multibyte_backend_diagnostics_without_losing_the_prefix(
    tmp_path,
):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="default",
        device="cpu",
        expected_backends=("pypdfium2",),
    )
    diagnostic = f"backend failed for {source}: ไทย🙂" + "🙂ไทย" * 100_000

    def convert(_path, **_kwargs):
        raise RuntimeError(diagnostic)

    record = run_scenario(scenario, convert=convert)
    message = record["error_message"]

    assert len(diagnostic.encode("utf-8")) > benchmark_matrix.MAX_RAW_RESULT_BYTES
    assert message.startswith("backend failed for pdf/report.pdf: ไทย🙂")
    assert message.endswith("…")
    assert len(
        json.dumps(message, ensure_ascii=False).encode("utf-8")
    ) <= benchmark_worker.MAX_ERROR_MESSAGE_JSON_BYTES
    assert len(message) <= benchmark_report.MAX_PUBLIC_TEXT_LENGTH
    assert message.encode("utf-8").decode("utf-8") == message
    assert str(source) not in message


@pytest.mark.parametrize(
    ("prefix", "repeated"),
    (
        pytest.param("backend failed: x", "\\", id="backslashes"),
        pytest.param("backend failed: xxx", "\x00", id="c0-controls"),
    ),
)
def test_worker_bounds_json_expanding_diagnostics_by_serialized_bytes(
    tmp_path,
    prefix,
    repeated,
):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="default",
        device="cpu",
        expected_backends=("pypdfium2",),
    )

    def convert(_path, **_kwargs):
        raise RuntimeError(prefix + repeated * benchmark_matrix.MAX_RAW_RESULT_BYTES)

    message = run_scenario(scenario, convert=convert)["error_message"]
    serialized = json.dumps(message, ensure_ascii=False).encode("utf-8")

    assert message.startswith(prefix)
    assert message.endswith("…")
    if repeated == "\x00":
        assert "\x00" not in message
        assert "�" in message
    assert len(serialized) == benchmark_worker.MAX_ERROR_MESSAGE_JSON_BYTES


def test_worker_sanitizes_surrogates_before_truncation_and_public_validation(
    tmp_path,
):
    source = tmp_path / "th-clean.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="language",
        source="pdf/th-clean.pdf",
        input_path=source,
        document_type="scanned-pdf",
        method="ocr",
        device="cpu",
        expected_backends=("ocrmac", "rapidocr"),
        language="thai",
    )

    def convert(_path, **_kwargs):
        raise NoEngineFor("no OCR engine \ud800 " + "x\ud800" * 100_000)

    record = run_scenario(scenario, convert=convert)
    message = record["error_message"]

    assert message.startswith("no OCR engine � ")
    assert message.endswith("…")
    assert "\ud800" not in message
    assert message.encode("utf-8").decode("utf-8") == message
    assert len(
        json.dumps(message, ensure_ascii=False).encode("utf-8")
    ) <= benchmark_worker.MAX_ERROR_MESSAGE_JSON_BYTES
    assert len(message) <= benchmark_report.MAX_PUBLIC_TEXT_LENGTH

    document = _public_result_document()
    document["metadata"]["scenario_plan"] = [scenario.public_plan()]
    document["samples"] = [record]

    assert validate_result_documents(
        [document],
        required_runs=((document["metadata"]["platform"], "cpu"),),
    )


def test_worker_preserves_a_wrapped_unsupported_script_as_no_engine_for(tmp_path):
    source = tmp_path / "th-clean.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="language",
        source="pdf/th-clean.pdf",
        input_path=source,
        document_type="scanned-pdf",
        method="ocr",
        device="cpu",
        expected_backends=("ocrmac", "rapidocr"),
        language="thai",
    )

    def convert(path, **_kwargs):
        try:
            raise NoEngineFor(f"no OCR engine for {path}")
        except NoEngineFor as error:
            raise ConversionError(str(error)) from error

    record = run_scenario(scenario, convert=convert)

    assert record == {
        "scenario": scenario.public_plan(),
        "status": "unsupported",
        "error_type": "NoEngineFor",
        "error_message": "no OCR engine for pdf/th-clean.pdf",
    }
    assert str(source) not in json.dumps(record)


def test_worker_excludes_route_or_marker_mismatches_from_timing(tmp_path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="default",
        device="cpu",
        expected_backends=("pypdfium2",),
        marker="EXPECTED MARKER",
    )

    record = run_scenario(
        scenario,
        convert=lambda _path, **_kwargs: SimpleNamespace(
            markdown="wrong document content", backend="docling"
        ),
    )

    assert record["status"] == "validation_error"
    assert record["backend"] == "docling"
    assert record["marker_verified"] is False
    assert "warm_samples_seconds" not in record
    assert "wrong document content" not in json.dumps(record)
    assert str(source) not in json.dumps(record)


def test_worker_expands_warm_samples_only_when_initial_variance_is_high(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("fixture", encoding="utf-8")
    scenario = Scenario(
        suite="format",
        source="text/notes.txt",
        input_path=source,
        document_type="direct-text",
        method="default",
        device="cpu",
        expected_backends=("plain",),
        marker="MARKER",
    )
    clock_values = iter(
        [
            0, 1_000_000_000,  # unreported warm-up
            2_000_000_000, 2_100_000_000,  # 0.1s: expand after third sample
            3_000_000_000, 4_000_000_000,
            5_000_000_000, 6_000_000_000,
            7_000_000_000, 8_000_000_000,
            9_000_000_000, 10_000_000_000,
            11_000_000_000, 12_000_000_000,
            13_000_000_000, 14_000_000_000,
        ]
    )

    record = run_scenario(
        scenario,
        convert=lambda _path, **_kwargs: SimpleNamespace(
            markdown="MARKER", backend="plain"
        ),
        clock_ns=lambda: next(clock_values),
        policy=TimingPolicy(min_sample_seconds=0, variance_ratio=0.10),
    )

    assert record["status"] == "success"
    assert record["adaptive_warm_repeats"] is True
    assert record["warm_samples_seconds"] == pytest.approx(
        [0.1, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    )


def test_worker_batches_fast_operations_before_dividing_by_operation_count(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("fixture", encoding="utf-8")
    scenario = Scenario(
        suite="format",
        source="text/notes.txt",
        input_path=source,
        document_type="direct-text",
        method="default",
        device="cpu",
        expected_backends=("plain",),
        marker="MARKER",
    )
    calls = []
    clock_values = iter(
        [
            0, 10_000_000,  # unreported warm-up
            1_000_000_000, 1_100_000_000, 1_300_000_000,
            2_000_000_000, 2_100_000_000, 2_300_000_000,
            3_000_000_000, 3_100_000_000, 3_300_000_000,
        ]
    )

    def convert(_path, **_kwargs):
        calls.append(1)
        return SimpleNamespace(markdown="MARKER", backend="plain")

    record = run_scenario(
        scenario,
        convert=convert,
        clock_ns=lambda: next(clock_values),
        policy=TimingPolicy(min_sample_seconds=0.25),
    )

    assert len(calls) == 7  # warm-up + two operations for each warm sample
    assert record["warm_operation_counts"] == [2, 2, 2]
    assert record["warm_samples_seconds"] == pytest.approx([0.15, 0.15, 0.15])


def test_worker_includes_only_observable_resource_peaks(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"fixture")
    scenario = Scenario(
        suite="format",
        source="pdf/report.pdf",
        input_path=source,
        document_type="born-digital-pdf",
        method="docling",
        device="cpu",
        expected_backends=("docling",),
        marker="MARKER",
    )

    class FakeSampler:
        def __init__(self):
            self.started = 0
            self.observations = 0

        def start(self):
            self.started += 1

        def observe(self):
            self.observations += 1

        def result(self):
            return {
                "peak_process_rss_mib": 321.5,
                "pytorch_allocator_peak_mib": None,
                "pytorch_allocator_peak_status": "not-applicable-for-cpu",
            }

    sampler = FakeSampler()
    record = run_scenario(
        scenario,
        convert=lambda _path, **_kwargs: SimpleNamespace(
            markdown="MARKER", backend="docling"
        ),
        resource_sampler=sampler,
        policy=TimingPolicy(min_sample_seconds=0, max_warm_repeats=3),
    )

    assert sampler.started == 1
    assert sampler.observations == 4  # initialization + three warm samples
    assert record["resources"] == {
        "peak_process_rss_mib": 321.5,
        "pytorch_allocator_peak_mib": None,
        "pytorch_allocator_peak_status": "not-applicable-for-cpu",
    }


def test_worker_scores_language_output_without_publishing_either_text(tmp_path):
    source = tmp_path / "th-clean.pdf"
    truth = tmp_path / "th-clean.txt"
    source.write_bytes(b"fixture")
    truth.write_text("ข้อความทดสอบ", encoding="utf-8")
    scenario = Scenario(
        suite="language",
        source="pdf/th-clean.pdf",
        input_path=source,
        document_type="scanned-pdf",
        method="ocr",
        device="cpu",
        expected_backends=("ocrmac",),
        language="thai",
        truth_path=truth,
    )

    record = run_scenario(
        scenario,
        convert=lambda _path, **_kwargs: SimpleNamespace(
            markdown="ข้อความทดสอบ", backend="ocrmac"
        ),
        policy=TimingPolicy(min_sample_seconds=0, max_warm_repeats=3),
    )

    assert record["quality"] == {"cer": 0.0, "cer_ns": 0.0, "cer_bag": 0.0}
    rendered = json.dumps(record, ensure_ascii=False)
    assert "ข้อความทดสอบ" not in rendered
    assert str(truth) not in rendered
