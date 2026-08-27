"""Security regressions for the public benchmark-promotion boundary.

Each mutation starts with the committed public benchmark evidence so the
failure pinpoints a changed value rather than an incomplete synthetic run.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import bench.matrix as benchmark_matrix
from bench.matrix import ResultStore
from bench.matrix_report import (
    ValidationError,
    promote_results,
    render_csv,
    render_markdown,
    validate_result_documents,
)


_PUBLISHED_RESULT = (
    Path(__file__).parents[1]
    / "bench"
    / "results"
    / "7203499e58bf8e6415b3190638d0f8a689f55924"
    / "macos-arm64-cpu.json"
)
_PUBLISHED_RESULTS = tuple(
    sorted(_PUBLISHED_RESULT.parent.glob("*.json"))
)
REPOSITORY = Path(__file__).resolve().parents[1]


def _result_document() -> dict[str, object]:
    return json.loads(_PUBLISHED_RESULT.read_text(encoding="utf-8"))


def _success_sample(document: dict[str, object], *, suite: str = "format") -> dict[str, object]:
    return next(
        sample
        for sample in document["samples"]
        if sample["status"] == "success" and sample["scenario"]["suite"] == suite
    )


def _replace_sample_plan(document: dict[str, object], sample: dict[str, object]) -> None:
    scenario = sample["scenario"]
    plan = document["metadata"]["scenario_plan"]
    for index, item in enumerate(plan):
        if item["id"] == scenario["id"]:
            plan[index] = deepcopy(scenario)
            return
    raise AssertionError("fixture sample was absent from its scenario plan")


def _set(document: dict[str, object], path: str, value: object) -> None:
    target: dict[str, object] = document
    *parents, field = path.split(".")
    for parent in parents:
        target = target[parent]
    target[field] = value


def _assert_rejected(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        validate_result_documents([document], required_runs=(("macos-arm64", "cpu"),))


def _assert_terminal_safe_error(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "duplicate JSON object key" in completed.stderr
    assert (
        "ไทย" in completed.stderr
        or "\\u0e44\\u0e17\\u0e22" in completed.stderr
    )
    for control in ("\x1b", "\x07", "\x85", "\u202e"):
        assert control not in completed.stderr
    for visible_escape in ("\\x1b", "\\x07", "\\x85", "\\u202e"):
        assert visible_escape in completed.stderr


def test_published_public_metadata_remains_accepted_by_the_closed_schema():
    """A schema hardening must retain real platform/version/hash evidence."""
    document = _result_document()

    rows = validate_result_documents(
        [document], required_runs=(("macos-arm64", "cpu"),)
    )

    assert rows


def test_all_committed_benchmark_documents_and_sanitized_output_round_trip(tmp_path):
    """Historical evidence must remain promotable after diagnostic stripping."""
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in _PUBLISHED_RESULTS]
    required_runs = tuple(
        (document["metadata"]["platform"], document["metadata"]["device"])
        for document in documents
    )

    rows = validate_result_documents(documents, required_runs=required_runs)
    output = tmp_path / "promoted"
    promote_results(
        _PUBLISHED_RESULTS,
        output,
        input_sha256=tuple(
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in _PUBLISHED_RESULTS
        ),
        required_runs=required_runs,
    )
    promoted = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(output.glob("*.json"))
    ]

    assert len(rows) == sum(len(document["samples"]) for document in documents)
    assert validate_result_documents(promoted, required_runs=required_runs)


def test_promotion_rejects_result_changed_after_trusted_digest_was_recorded(tmp_path):
    document = _result_document()
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")
    trusted_sha256 = hashlib.sha256(raw.read_bytes()).hexdigest()
    sample = _success_sample(document)
    sample_count = len(sample["warm_samples_seconds"])
    sample["warm_samples_seconds"] = [0.000001] * sample_count
    sample["warm_median_seconds"] = 0.000001
    sample["warm_range_seconds"] = 0.0
    sample["output_sha256"] = "0" * 64
    raw.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "promoted"

    with pytest.raises(ValidationError, match="SHA-256 does not match"):
        promote_results(
            [raw],
            output,
            input_sha256=(trusted_sha256,),
            required_runs=(("macos-arm64", "cpu"),),
        )

    assert not output.exists()


def test_promotion_accepts_uppercase_trusted_digest_from_powershell(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_result_document()), encoding="utf-8")
    output = tmp_path / "promoted"

    promote_results(
        [raw],
        output,
        input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest().upper(),),
        required_runs=(("macos-arm64", "cpu"),),
    )

    assert (output / "macos-arm64-cpu.json").is_file()


def test_promotion_rejects_casefolded_filename_collisions_before_writing(tmp_path):
    first = _result_document()
    second = deepcopy(first)
    first["metadata"]["platform"] = "macOS-arm64"
    second["metadata"]["platform"] = "MacOS-arm64"
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")
    second_path.write_text(json.dumps(second), encoding="utf-8")
    output = tmp_path / "promoted"

    with pytest.raises(ValidationError, match="promotion filename collision"):
        promote_results(
            [first_path, second_path],
            output,
            input_sha256=(
                hashlib.sha256(first_path.read_bytes()).hexdigest(),
                hashlib.sha256(second_path.read_bytes()).hexdigest(),
            ),
            required_runs=(("macOS-arm64", "cpu"), ("MacOS-arm64", "cpu")),
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("path", "value"),
    (
        pytest.param("configuration_fingerprint", "A" * 64, id="uppercase-config-digest"),
        pytest.param("metadata.commit", "a" * 39, id="short-commit"),
        pytest.param("metadata.benchmark_schema_version", True, id="boolean-schema-version"),
        pytest.param("metadata.cpu_count", True, id="boolean-counter"),
        pytest.param("metadata.total_memory_mib", -1.0, id="negative-memory"),
        pytest.param("metadata.python", 3.12, id="numeric-package-version"),
    ),
)
def test_promotion_rejects_malformed_public_metadata_scalars(path, value):
    """Production break: weak scalar typing lets forged provenance publish."""
    document = _result_document()
    _set(document, path, value)

    _assert_rejected(document)


@pytest.mark.parametrize("field", ("purpose", "suite", "device"))
def test_promotion_rejects_unhashable_metadata_enums_as_validation_errors(field):
    """Malformed enum types must not escape the public validation boundary."""
    document = _result_document()
    document["metadata"][field] = []

    _assert_rejected(document)


def test_promotion_bounds_deeply_nested_untrusted_data():
    """An adversarial nested value must become ValidationError, never recursion failure."""
    document = _result_document()
    nested: list[object] = []
    cursor = nested
    for _ in range(1_200):
        child: list[object] = []
        cursor.append(child)
        cursor = child
    document["metadata"]["os"]["version"] = nested

    _assert_rejected(document)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        pytest.param("output_sha256", "0" * 63, id="short-output-digest"),
        pytest.param("initialization_seconds", float("inf"), id="infinite-initialization"),
        pytest.param("warm_operation_counts", [True, True, True], id="boolean-operation-counter"),
        pytest.param("peak_process_rss_mib", -0.25, id="negative-rss"),
        pytest.param("pytorch_allocator_peak_mib", float("nan"), id="nan-allocator-peak"),
    ),
)
def test_promotion_rejects_nonfinite_or_untyped_published_sample_scalars(path, value):
    """Production break: non-finite or boolean metrics can enter public reports."""
    document = _result_document()
    sample = _success_sample(document)
    if path in sample:
        sample[path] = value
    else:
        sample["resources"][path] = value

    _assert_rejected(document)


@pytest.mark.parametrize("metric", ("cer", "cer_ns", "cer_bag"))
@pytest.mark.parametrize("value", (-0.01, 1.01, True, float("inf")))
def test_promotion_rejects_invalid_language_quality_metrics(metric, value):
    """Production break: impossible quality scores can make language claims."""
    document = _result_document()
    sample = _success_sample(document, suite="language")
    sample["quality"][metric] = value

    _assert_rejected(document)


def test_promotion_rejects_null_resource_measurement_with_available_status():
    """Production break: a null value must not contradict an available sampler."""
    document = _result_document()
    resources = _success_sample(document)["resources"]
    resources["pytorch_allocator_peak_mib"] = None
    resources["pytorch_allocator_peak_status"] = "available"

    _assert_rejected(document)


@pytest.mark.parametrize(
    ("container", "field"),
    (
        pytest.param("sample", "output_sha256", id="output-digest"),
        pytest.param("sample", "adaptive_warm_repeats", id="adaptive-repeat-flag"),
        pytest.param("resources", "process_rss_status", id="process-rss-status"),
    ),
)
def test_promotion_requires_every_success_evidence_field(container, field):
    """Success rows cannot silently omit provenance or resource-status data."""
    document = _result_document()
    sample = _success_sample(document)
    target = sample if container == "sample" else sample["resources"]
    del target[field]

    _assert_rejected(document)


def test_promotion_rejects_unknown_nested_resource_status():
    """Production break: an untrusted nested enum can become public evidence."""
    document = _result_document()
    _success_sample(document)["resources"]["process_rss_status"] = "collector-debug-path"

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "/opt/benchmark/private.json",
        "(/srv/runner/secret)",
        "~",
        "~/secret.json",
        "~benchmark/secret.json",
        r"C:\benchmark\secret.json",
        r"C:secret.json",
        r"\\server\share\secret.json",
        r"\\?\C:\benchmark\secret.json",
        "file:///private/secret.json",
        "file:%2f%2fprivate%2fsecret.json",
    ),
)
def test_promotion_recursively_rejects_private_path_spellings(private_value):
    """Production break: path spelling variants leak local benchmark locations."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        pytest.param("/etc/passwd", id="posix-etc"),
        pytest.param("/opt/d2md/results.json", id="posix-opt"),
        pytest.param("/srv/benchmark/results.json", id="posix-srv"),
        pytest.param("/mnt/results/output.json", id="posix-mnt"),
        pytest.param("/Users/alice/results.json", id="posix-users"),
        pytest.param("root=/etc/passwd", id="equals-before-posix"),
        pytest.param("root;/opt/d2md/results.json", id="semicolon-before-posix"),
        pytest.param("root|/srv/benchmark/results.json", id="pipe-before-posix"),
        pytest.param("root(/mnt/results/output.json)", id="paren-before-posix"),
        pytest.param("root[/Users/alice/results.json]", id="bracket-before-posix"),
        pytest.param("root=%2Fetc%2Fpasswd", id="encoded-posix"),
        pytest.param("root=%252Fopt%252Fd2md", id="nested-encoded-posix"),
        pytest.param("root./etc/passwd", id="dot-before-posix"),
        pytest.param("root-/opt/private.json", id="dash-before-posix"),
        pytest.param("root_/srv/private.json", id="underscore-before-posix"),
        pytest.param("~", id="bare-tilde"),
        pytest.param("~runner", id="bare-named-home"),
        pytest.param("%7Erunner", id="encoded-bare-named-home"),
        pytest.param("~+", id="current-directory-tilde"),
        pytest.param("root=~+", id="delimited-current-directory-tilde"),
        pytest.param("%7E%2B", id="encoded-current-directory-tilde"),
        pytest.param(
            "root=%257E%252B",
            id="nested-encoded-current-directory-tilde",
        ),
        pytest.param("~/results.json", id="home-relative"),
        pytest.param("~runner/results.json", id="named-home-relative"),
        pytest.param("root=~runner/results.json", id="delimited-named-home"),
        pytest.param(r"C:\results\secret.json", id="windows-absolute-backslash"),
        pytest.param("C:/results/secret.json", id="windows-absolute-slash"),
        pytest.param("C:secret.json", id="windows-drive-relative"),
        pytest.param(r"root=C:\results\secret.json", id="delimited-windows"),
        pytest.param("root=C%3A%5Cresults%5Csecret.json", id="encoded-windows"),
        pytest.param(
            "root=C%253A%255Cresults%255Csecret.json",
            id="nested-encoded-windows",
        ),
        pytest.param(r"\\server\share\secret.json", id="unc-backslash"),
        pytest.param(r"\Users\runner\secret.json", id="windows-root-relative"),
        pytest.param(
            r"root=\Users\runner\secret.json",
            id="delimited-windows-root-relative",
        ),
        pytest.param(
            "root=%5CUsers%5Crunner%5Csecret.json",
            id="encoded-windows-root-relative",
        ),
        pytest.param("//server/share/secret.json", id="network-slash"),
        pytest.param(r"root;\\server\share\secret.json", id="delimited-unc"),
        pytest.param("root|//server/share/secret.json", id="delimited-network"),
        pytest.param("root-//server/share/secret.json", id="dash-before-network"),
        pytest.param(
            "root=%5C%5Cserver%5Cshare%5Csecret.json",
            id="encoded-unc",
        ),
        pytest.param(
            "root=%252F%252Fserver%252Fshare%252Fsecret.json",
            id="nested-encoded-network",
        ),
    ),
)
def test_promotion_rejects_absolute_or_network_paths_after_normalization(
    private_value,
):
    """Delimiters and repeated encoding cannot hide a host-local path."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "url_value",
    (
        "https://example.test/benchmark/7203499",
        "http://example.test/benchmark/7203499",
        "file:///var/tmp/benchmark.json",
        "HtTpS://example.test/mixed-case",
        "FiLe:///var/tmp/mixed-case.json",
        "ftp://example.test/results.json",
        "s3://benchmark-bucket/results.json",
        "ssh://runner.example.test/results.json",
        "mailto:benchmark@example.test",
        "data:text/plain,benchmark",
        "urn:d2md:benchmark",
        "provenance=(https://example.test/results.json)",
        "source=https://example.test/results.json",
    ),
)
def test_promotion_rejects_every_url_or_url_scheme_public_value(url_value):
    """Public benchmark artifacts deliberately contain no clickable provenance."""
    document = _result_document()
    document["metadata"]["os"]["version"] = url_value

    _assert_rejected(document)


def test_promotion_accepts_the_d2md_dependency_identity():
    document = _result_document()
    document["metadata"]["commit"] = "a" * 40
    document["metadata"]["dependencies"] = {"d2md": "0.1.0"}

    assert validate_result_documents(
        [document], required_runs=(("macos-arm64", "cpu"),)
    )


def test_promotion_accepts_historical_dependency_identity():
    document = _result_document()

    assert validate_result_documents(
        [document], required_runs=(("macos-arm64", "cpu"),)
    )


def test_promotion_rejects_doc2md_dependency_identity_after_rebrand():
    document = _result_document()
    document["metadata"]["commit"] = "a" * 40

    _assert_rejected(document)


def test_promotion_rejects_mixed_project_dependency_identities():
    document = _result_document()
    document["metadata"]["dependencies"]["d2md"] = "0.1.0"

    _assert_rejected(document)


@pytest.mark.parametrize(
    "safe_value",
    ("profile: release build", "my_file: release build", "100% complete"),
)
def test_promotion_allows_safe_non_url_ordinary_text(safe_value):
    """Ordinary colons and percent signs are not URL or path provenance."""
    document = _result_document()
    document["metadata"]["os"]["version"] = safe_value

    assert validate_result_documents(
        [document], required_runs=(("macos-arm64", "cpu"),)
    )


@pytest.mark.parametrize(
    "private_value",
    (
        "https://example.test/public,/Users/alice/private.json",
        "//srv/runner/secret",
    ),
)
def test_promotion_rejects_private_paths_adjacent_to_urls_or_network_roots(private_value):
    """A legitimate URL must not erase a punctuation-prefixed local path."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "https://example.test/public=/Users/alice/private.json",
        "https://example.test/public;/Users/alice/private.json",
        "https://example.test/%2FUsers/alice/private.json",
    ),
)
def test_promotion_rejects_local_path_payloads_inside_whole_https_urls(private_value):
    """A complete URL cannot bypass path checks through route delimiters or encoding."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "https://example.test/?next=//srv/runner/secret",
        "https://example.test/a,//srv/runner/secret",
        "https://example.test/#next://srv/runner/secret",
    ),
)
def test_promotion_rejects_network_roots_inside_https_components(private_value):
    """Network roots remain private without treating ordinary URL slashes as paths."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "https://C:%5CUsers%5Calice@example.test/",
        "https://user:%2FUsers%2Falice%2Fsecret@example.test/",
        "https://user:%5C%5Cserver%5Cshare%5Csecret@example.test/",
    ),
)
def test_promotion_rejects_https_userinfo(private_value):
    """Public benchmark provenance has no legitimate HTTPS userinfo use case."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "https://C:%5CUsers%5Calice/",
        "https://%5C%5Cserver%5Cshare/",
    ),
)
def test_promotion_rejects_local_path_payloads_in_https_authority(private_value):
    """Decoded HTTPS authorities must still be valid hosts, never local paths."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "private_value",
    (
        "https://example.test/path:/Users/alice/private.json",
        "https://example.test/?next:C:%5CUsers%5Calice%5Csecret.txt",
        "https://example.test/#next=file:%2Fprivate%2Fsecret.txt",
        "https://example.test/#(/Users/alice/private.json)",
        "https://example.test/?next=%252525252FUsers%252525252Falice",
        "%252FUsers%252Falice",
        "file%253A%252F%252Fprivate%252Fsecret.txt",
        "C%253A%255CUsers%255Calice%255Csecret.txt",
        "%25ZZ",
    ),
)
def test_promotion_rejects_normalized_local_path_payloads(private_value):
    """Nested and repeatedly encoded local paths remain private after normalization."""
    document = _result_document()
    document["metadata"]["os"]["version"] = private_value

    _assert_rejected(document)


@pytest.mark.parametrize(
    "encoded_url",
    (
        "https%3A%2F%2Fexample.test%2Fresults.json",
        "HTTPS%253A%252F%252Fexample.test%252Fresults.json",
        "https%25253A%25252F%25252Fexample.test%25252Fresults.json",
        "http%253A%252F%252Fexample.test%252Fresults.json",
        "file%3A%2F%2F%2Fvar%2Ftmp%2Fresults.json",
        "FiLe%253A%252F%252F%252Fvar%252Ftmp%252Fresults.json",
        "source%3Dhttps%253A%252F%252Fexample.test%252Fresults.json",
    ),
)
def test_promotion_rejects_url_schemes_after_percent_decoding_until_stable(
    encoded_url,
):
    """Nested encoding cannot turn forbidden provenance into public text."""
    document = _result_document()
    document["metadata"]["os"]["version"] = encoded_url

    _assert_rejected(document)


@pytest.mark.parametrize(
    "fixture_name",
    (
        "input:a//Users/alice/private.json",
        "input:a/../../Users/alice/private.json",
        "input:public\u2028name.txt",
    ),
)
def test_promotion_validates_dynamic_fixture_hash_keys_recursively(fixture_name):
    """Public hash labels cannot hide controls or path traversal in map keys."""
    document = _result_document()
    document["metadata"]["fixture_hashes"][fixture_name] = "a" * 64

    _assert_rejected(document)


def test_csv_keeps_formula_like_public_labels_as_literal_text():
    """Production break: CSV rendering must retain spreadsheet formula neutralization."""
    rows = validate_result_documents(
        [_result_document()], required_runs=(("macos-arm64", "cpu"),)
    )
    rows[0]["platform"] = "=HYPERLINK(\"https://evil.test\")"

    assert "'=HYPERLINK" in render_csv(rows)


def test_validation_or_markdown_escaping_prevents_table_formatting_injection():
    """Production break: a forged source must not become Markdown syntax."""
    document = _result_document()
    sample = _success_sample(document)
    payload = "safe|cell\nnew-row [link](https://evil.test) ![image](x) <img src=x> `code`"
    original_id = sample["scenario"]["id"]
    sample["scenario"]["source"] = payload
    sample["scenario"]["id"] = f"format:{payload}:default:cpu"
    for index, item in enumerate(document["metadata"]["scenario_plan"]):
        if item["id"] == original_id:
            document["metadata"]["scenario_plan"][index] = deepcopy(sample["scenario"])
            break
    else:
        raise AssertionError("fixture sample was absent from its scenario plan")

    try:
        rows = validate_result_documents(
            [document], required_runs=(("macos-arm64", "cpu"),)
        )
    except ValidationError:
        return

    markdown = render_markdown(rows, commit=document["metadata"]["commit"])
    for marker in ("[link]", "![image]", "<img", "`code`", "\nnew-row"):
        assert marker not in markdown


@pytest.mark.parametrize("control", ("\x00", "\x1f", "\x7f", "\u2028", "\u2029"))
def test_promotion_rejects_controls_before_public_json_csv_or_markdown(control):
    """Production break: control characters make public artifacts ambiguous."""
    document = _result_document()
    document["metadata"]["platform"] = f"macos-arm64{control}"

    _assert_rejected(document)


def test_promotion_rejects_bidi_format_controls_in_public_metadata():
    document = _result_document()
    document["metadata"]["os"]["version"] = "public\u202evalue"

    _assert_rejected(document)


@pytest.mark.parametrize(
    "encoded_control",
    (
        "%00",
        "%1F",
        "%7F",
        "%C2%80",
        "%E2%80%A8",
        "%E2%80%A9",
        "%E2%80%AE",
    ),
)
def test_promotion_rejects_percent_encoded_public_controls(encoded_control):
    """Percent normalization cannot introduce controls after the raw-text check."""
    document = _result_document()
    document["metadata"]["os"]["version"] = f"public{encoded_control}value"

    _assert_rejected(document)


def test_promotion_rejects_surrogates_before_creating_output(tmp_path):
    document = _result_document()
    document["metadata"]["os"]["version"] = "public\ud800value"
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "promoted"

    with pytest.raises(ValidationError, match="control character"):
        promote_results(
            [raw],
            output,
            input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest(),),
            required_runs=(("macos-arm64", "cpu"),),
        )

    assert not output.exists()


def test_matrix_run_cli_escapes_duplicate_json_key_errors(tmp_path):
    hostile = "ไทย\x1b]8;;https://invalid\x07\x85\u202e"
    encoded_key = json.dumps(hostile)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        f'{{"documents":[],{encoded_key}:1,{encoded_key}:2}}',
        encoding="utf-8",
    )

    run_matrix_with_fixed_commit = (
        "import bench.matrix as matrix; "
        "matrix.current_commit = lambda: 'a' * 40; "
        "raise SystemExit(matrix.main())"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            run_matrix_with_fixed_commit,
            "run",
            "--format-manifest",
            str(manifest),
            "--suite",
            "format",
            "--platform",
            "test",
            "--device",
            "cpu",
            "--output",
            str(tmp_path / "raw.json"),
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    _assert_terminal_safe_error(completed)


def test_matrix_promote_cli_escapes_duplicate_json_key_errors(tmp_path):
    hostile = "ไทย\x1b]8;;https://invalid\x07\x85\u202e"
    encoded_key = json.dumps(hostile)
    raw = tmp_path / "raw.json"
    raw.write_text(f'{{{encoded_key}:1,{encoded_key}:2}}', encoding="utf-8")
    run_module_with_clean_checkout = (
        "import runpy; import bench.matrix as matrix; "
        "matrix.current_clean_commit = lambda: 'a' * 40; "
        "runpy.run_module('bench.matrix_report', run_name='__main__')"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            run_module_with_clean_checkout,
            "promote",
            "--input",
            str(raw),
            hashlib.sha256(raw.read_bytes()).hexdigest(),
            "--output",
            str(tmp_path / "promoted"),
            "--format-manifest",
            str(tmp_path / "manifest.json"),
            "--corpus",
            str(tmp_path / "corpus"),
            "--require",
            "test:cpu",
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
    )

    _assert_terminal_safe_error(completed)


@pytest.mark.parametrize("token", ("NaN", "Infinity", "-Infinity"))
def test_promotion_rejects_nonstandard_json_number_tokens_at_input_parse_time(tmp_path, token):
    """Production break: Python's permissive JSON decoder accepts non-JSON numbers."""
    document = _result_document()
    raw = tmp_path / "raw.json"
    serialized = json.dumps(document)
    raw.write_text(
        serialized.replace('"cpu_count": 8', f'"cpu_count": {token}', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest(),),
            required_runs=(("macos-arm64", "cpu"),),
        )


def test_promotion_rejects_oversized_raw_result_before_json_parsing(
    tmp_path, monkeypatch
):
    raw = tmp_path / "raw.json"
    raw.write_bytes(b" " * (benchmark_matrix.MAX_RAW_RESULT_BYTES + 1))
    parser_called = False

    def parse_json(_text):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input reached the JSON parser")

    monkeypatch.setattr(benchmark_matrix, "load_strict_json", parse_json)

    with pytest.raises(ValidationError, match="exceeds"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest(),),
            required_runs=(("macos-arm64", "cpu"),),
        )

    assert not parser_called


def test_promotion_converts_missing_raw_result_to_validation_error(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(
        ValidationError,
        match="cannot parse benchmark result missing.json: cannot read JSON file",
    ):
        promote_results(
            [missing],
            tmp_path / "promoted",
            input_sha256=("0" * 64,),
            required_runs=(("macos-arm64", "cpu"),),
        )


def test_promotion_converts_deeply_nested_raw_json_to_validation_error(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text("[" * 10_000 + "0" + "]" * 10_000, encoding="utf-8")

    with pytest.raises(ValidationError, match="nesting"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest(),),
            required_runs=(("macos-arm64", "cpu"),),
        )


def test_promotion_converts_invalid_utf8_to_validation_error(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_bytes(b'{"metadata":"\xff"}')

    with pytest.raises(ValidationError, match="not valid UTF-8"):
        promote_results(
            [raw],
            tmp_path / "promoted",
            input_sha256=(hashlib.sha256(raw.read_bytes()).hexdigest(),),
            required_runs=(("macos-arm64", "cpu"),),
        )


def test_result_store_rejects_oversized_raw_result_before_json_parsing(
    tmp_path, monkeypatch
):
    raw = tmp_path / "raw.json"
    raw.write_bytes(b" " * (benchmark_matrix.MAX_RAW_RESULT_BYTES + 1))
    parser_called = False

    def parse_json(_text):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input reached the JSON parser")

    monkeypatch.setattr(benchmark_matrix, "load_strict_json", parse_json)

    with pytest.raises(ValueError, match="invalid raw benchmark JSON.*exceeds"):
        ResultStore.open(raw, fingerprint="same", resume=True)

    assert not parser_called


def test_result_store_accepts_valid_raw_result_at_exact_byte_limit(tmp_path):
    raw = tmp_path / "raw.json"
    serialized = json.dumps(
        {
            "schema_version": 1,
            "configuration_fingerprint": "same",
            "metadata": {},
            "samples": [],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    raw.write_bytes(
        serialized
        + b" " * (benchmark_matrix.MAX_RAW_RESULT_BYTES - len(serialized))
    )

    store = ResultStore.open(raw, fingerprint="same", resume=True)

    assert store.document["samples"] == []


def test_result_store_refuses_to_serialize_nonfinite_values(tmp_path):
    """Production break: raw resumable results must never write non-standard JSON."""
    with pytest.raises(ValueError):
        ResultStore.create(
            tmp_path / "raw.json",
            fingerprint="f" * 64,
            metadata={"cpu_count": float("nan")},
        )


@pytest.mark.parametrize(
    "raw_document",
    (
        '{"schema_version": 1, "configuration_fingerprint": "same", "metadata": {"value": NaN}, "samples": []}',
        '{"schema_version": 1, "configuration_fingerprint": "same", "metadata": {"value": Infinity}, "samples": []}',
        '{"schema_version": 1, "configuration_fingerprint": "same", "metadata": {"value": -Infinity}, "samples": []}',
        '{"schema_version": 1, "schema_version": 1, "configuration_fingerprint": "same", "metadata": {}, "samples": []}',
    ),
)
def test_result_store_open_rejects_nonstandard_or_duplicate_raw_json(tmp_path, raw_document):
    """Resumable raw results use the same strict JSON contract as promotion."""
    raw = tmp_path / "raw.json"
    raw.write_text(raw_document, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid raw benchmark JSON"):
        ResultStore.open(raw, fingerprint="same", resume=True)
