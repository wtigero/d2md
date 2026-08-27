"""Regressions for bounded heavy-backend subprocess isolation.

The suite is intentionally test-first.  It specifies three observable layers:
conversion routing, the worker's bounded spool protocol, and reusable process
lifecycle behavior exercised with an independent Python helper.
"""

import importlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
import signal
import sys
import textwrap
import threading
import time
import zipfile

import pytest

from d2md.convert import ConversionError, ConversionLimits, UNLIMITED_LIMITS, convert


convert_module = importlib.import_module("d2md.convert")


@pytest.fixture
def protocol_worker(tmp_path):
    script = tmp_path / "protocol_worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import sys
            import time

            behavior = sys.argv[1]
            for line in sys.stdin.buffer:
                request = json.loads(line)
                if behavior == "crash":
                    raise SystemExit(7)
                if behavior == "timeout":
                    time.sleep(2)
                if behavior == "oversized":
                    sys.stdout.write("x" * 8192 + "\\n")
                    sys.stdout.flush()
                    continue
                if behavior == "deep":
                    sys.stdout.write("[" * 4000 + "0\\n")
                    sys.stdout.flush()
                    continue
                if behavior == "truncated":
                    sys.stdout.write('{"v":1\\n')
                    sys.stdout.flush()
                    continue
                if behavior == "bad_error":
                    response = {
                        "v": 1,
                        "id": request["id"],
                        "status": "error",
                        "code": ["not", "a", "code"],
                        "message": "reported error",
                    }
                    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
                    sys.stdout.flush()
                    continue
                if behavior == "long_error":
                    response = {
                        "v": 1,
                        "id": request["id"],
                        "status": "error",
                        "code": "backend_failure",
                        "message": "x" * 2000,
                    }
                    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
                    sys.stdout.flush()
                    continue
                body = (
                    f"telemetry={os.environ.get('ORT_DISABLE_TELEMETRY', 'missing')}"
                    if behavior == "environment"
                    else "healthy backend output with enough text"
                )
                result = Path(request["result_path"])
                encoded = b"\\xff" if behavior == "invalid_utf8" else body.encode("utf-8")
                if behavior != "missing":
                    result.write_bytes(encoded)
                if behavior == "digits":
                    response = (
                        '{"v":1,"id":'
                        + json.dumps(request["id"])
                        + ',"status":"ok","result_path":'
                        + json.dumps(str(result))
                        + ',"chars":'
                        + "9" * 5000
                        + ',"bytes":1}\\n'
                    )
                    sys.stdout.write(response)
                    sys.stdout.flush()
                    continue
                response = {
                    "v": 1,
                    "id": request["id"],
                    "status": "ok",
                    "result_path": str(result),
                    "chars": 1 if behavior == "invalid_utf8" else len(body),
                    "bytes": len(encoded),
                }
                if behavior == "extra":
                    response["unexpected"] = True
                if behavior == "stale":
                    response["id"] = "0" * 32
                if behavior == "partial":
                    response["bytes"] += 1
                if behavior == "nan":
                    response["chars"] = float("nan")
                if behavior == "infinity":
                    response["chars"] = float("inf")
                serialized = json.dumps(response, separators=(",", ":"))
                if behavior == "duplicate":
                    serialized = serialized.replace(
                        '"status":"ok"',
                        '"status":"error","status":"ok"',
                        1,
                    )
                sys.stdout.write(serialized + "\\n")
                sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    return script


def test_backend_deadline_is_bounded_by_default_and_explicitly_unlimited():
    assert ConversionLimits().max_backend_seconds == 1_800
    assert UNLIMITED_LIMITS.max_backend_seconds is None


def test_backend_deadline_does_not_shift_existing_positional_limit_fields():
    limits = ConversionLimits(1, 2, 3, 4, 5, 6, 7, 8)

    assert limits.max_archive_members == 7
    assert limits.max_archive_bytes == 8
    assert limits.max_backend_seconds == 1_800


def test_markitdown_route_sends_every_boundary_to_isolated_worker(
    tmp_path, monkeypatch
):
    source = tmp_path / "notes.bin"
    source.write_bytes(b"ordinary unknown input")
    calls = []
    validated = frozenset({"pdf"})
    monkeypatch.setattr(
        convert_module, "_validate_input", lambda *args: validated
    )

    def fake_isolated(backend, path, **options):
        calls.append((backend, path, options))
        return "isolated output with enough usable text"

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_isolated)
    limits = ConversionLimits(max_output_chars=123, max_backend_seconds=7)

    result = convert(source, limits=limits)

    assert result.backend == "markitdown"
    assert len(calls) == 1
    backend, snapshot, options = calls[0]
    assert backend == "markitdown"
    assert snapshot != source
    assert snapshot.name.startswith("d2md-input-")
    assert options == {
        "max_output_chars": 123,
        "timeout_seconds": 7,
        "validated_formats": validated,
        "script": None,
        "force_ocr": False,
        "device": "auto",
        "ocr_enabled": False,
    }


def test_docling_route_sends_every_option_to_isolated_worker(tmp_path, monkeypatch):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"fake")
    calls = []
    monkeypatch.setattr(convert_module, "ensure_docling_available", lambda: None)
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)
    monkeypatch.setattr(convert_module, "_validate_input", lambda *args: frozenset())
    monkeypatch.setattr(convert_module, "script_of", lambda *args, **kwargs: "thai")
    monkeypatch.setattr(
        convert_module,
        "_via_pypdfium2",
        lambda *args, **kwargs: [
            "ประกนอบตเหตสวนบคคล ระบบสงสนคาถงบานทวประเทศไทย " * 60
        ],
    )
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda backend, path, **options: calls.append((backend, path, options))
        or "isolated Docling output with enough usable text",
    )
    limits = ConversionLimits(max_output_chars=321, max_backend_seconds=9)

    result = convert(
        source,
        ocr=True,
        docling=True,
        device="cpu",
        limits=limits,
    )

    assert result.backend == "docling+ocr"
    assert len(calls) == 1
    backend, snapshot, options = calls[0]
    assert backend == "docling"
    assert snapshot != source
    assert options == {
        "max_output_chars": 321,
        "timeout_seconds": 9,
        "validated_formats": frozenset(),
        "script": "thai",
        "force_ocr": True,
        "device": "cpu",
        "ocr_enabled": True,
    }


def test_exact_unicode_limit_succeeds_and_reuses_a_healthy_worker(tmp_path):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "unicode.txt"
    body = "ไทย e\u0301 😀 — reusable worker\n"
    source.write_text(body, encoding="utf-8")
    dispatcher = BackendDispatcher()
    try:
        first = dispatcher.run(
            "markitdown",
            source,
            max_output_chars=len(body),
            timeout_seconds=30,
        )
        first_pid = dispatcher.worker_pid("markitdown")
        second = dispatcher.run(
            "markitdown",
            source,
            max_output_chars=len(body),
            timeout_seconds=30,
        )

        assert first == body
        assert second == body
        assert first_pid is not None
        assert dispatcher.worker_pid("markitdown") == first_pid
    finally:
        dispatcher.close()


def test_backend_worker_disables_onnx_telemetry_before_child_imports(
    tmp_path, protocol_worker, monkeypatch
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "environment",
        ]
    )
    try:
        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=100,
            timeout_seconds=10,
        ) == "telemetry=1"
    finally:
        dispatcher.close()


def test_malformed_response_is_not_retried_and_next_call_replaces_worker(
    tmp_path, protocol_worker
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    launches = []

    def command(_backend):
        behavior = "extra" if not launches else "success"
        launches.append(behavior)
        return [sys.executable, "-u", str(protocol_worker), behavior]

    dispatcher = BackendDispatcher(command_factory=command)
    try:
        with pytest.raises(ConversionError, match="invalid"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert launches == ["extra"]
        assert dispatcher.worker_pid("markitdown") is None

        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=100,
            timeout_seconds=2,
        ) == "healthy backend output with enough text"
        assert launches == ["extra", "success"]
    finally:
        dispatcher.close()


def test_malformed_error_response_is_rejected(tmp_path, protocol_worker):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "bad_error",
        ]
    )
    try:
        with pytest.raises(ConversionError, match="invalid error response"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
    finally:
        dispatcher.close()


@pytest.mark.parametrize(
    "behavior",
    ("digits", "duplicate", "nan", "infinity", "deep", "truncated"),
)
def test_strict_json_failure_retires_the_worker(
    tmp_path, protocol_worker, behavior
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            behavior,
        ]
    )
    try:
        with pytest.raises(ConversionError, match="not valid JSON"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_json_recursion_failure_retires_the_worker(
    tmp_path, protocol_worker, monkeypatch
):
    import d2md._backend_process as backend_process

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = backend_process.BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "success",
        ]
    )

    def recursive_failure(*_args, **_kwargs):
        raise RecursionError("hostile nesting")

    monkeypatch.setattr(backend_process.json, "loads", recursive_failure)
    try:
        with pytest.raises(ConversionError, match="not valid JSON"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


@pytest.mark.parametrize(
    ("failure", "message", "timeout"),
    (
        ("crash", "closed its protocol pipe", 2),
        ("timeout", "timed out", 0.05),
        ("stale", "stale request ID", 2),
        ("oversized", "response frame is too large", 2),
    ),
)
def test_worker_fault_is_not_retried_and_next_call_uses_a_replacement(
    tmp_path, protocol_worker, failure, message, timeout
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    launches = []

    def command(_backend):
        behavior = failure if not launches else "success"
        launches.append(behavior)
        return [sys.executable, "-u", str(protocol_worker), behavior]

    dispatcher = BackendDispatcher(command_factory=command)
    try:
        with pytest.raises(ConversionError, match=message):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=timeout,
            )
        assert launches == [failure]
        assert dispatcher.worker_pid("markitdown") is None

        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=100,
            timeout_seconds=2,
        ) == "healthy backend output with enough text"
        assert launches == [failure, "success"]
    finally:
        dispatcher.close()


def test_limit_plus_one_retires_worker_without_truncated_success(tmp_path):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "unicode.txt"
    body = "ไทย e\u0301 😀 — reusable worker\n"
    source.write_text(body, encoding="utf-8")
    dispatcher = BackendDispatcher()
    try:
        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=len(body),
            timeout_seconds=10,
        ) == body
        original_pid = dispatcher.worker_pid("markitdown")

        with pytest.raises(ConversionError, match="output limit exceeded"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=len(body) - 1,
                timeout_seconds=10,
            )
        assert dispatcher.worker_pid("markitdown") is None

        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=len(body),
            timeout_seconds=10,
        ) == body
        assert dispatcher.worker_pid("markitdown") != original_pid
    finally:
        dispatcher.close()


@pytest.mark.parametrize(
    ("behavior", "message"),
    (
        ("invalid_utf8", "not valid UTF-8"),
        ("missing", "result is missing"),
        ("partial", "byte count does not match"),
    ),
)
def test_invalid_or_partial_result_is_rejected(
    tmp_path, protocol_worker, behavior, message
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            behavior,
        ]
    )
    try:
        with pytest.raises(ConversionError, match=message):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_backend_error_text_is_bounded(tmp_path, protocol_worker):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "long_error",
        ]
    )
    try:
        with pytest.raises(ConversionError) as raised:
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert len(str(raised.value).encode("utf-8")) <= 2_048
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_real_backend_failure_is_stable_and_does_not_expose_paths(tmp_path):
    from d2md._backend_process import BackendDispatcher

    private = tmp_path / "private-model-cache-secret.docx"
    with zipfile.ZipFile(private, "w") as archive:
        archive.writestr("not-word/document.xml", "invalid office package")
    dispatcher = BackendDispatcher()
    try:
        with pytest.raises(ConversionError) as raised:
            dispatcher.run(
                "markitdown",
                private,
                max_output_chars=100,
                timeout_seconds=10,
            )
        assert str(raised.value) == "markitdown backend failed"
        assert str(private) not in str(raised.value)
        assert private.name not in str(raised.value)
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_worker_spawn_failure_is_a_safe_conversion_error(tmp_path):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            str(tmp_path / "missing-python-interpreter")
        ]
    )
    try:
        with pytest.raises(ConversionError, match="cannot start backend worker"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
    finally:
        dispatcher.close()


@pytest.mark.parametrize(
    "timeout",
    (float("nan"), float("inf"), float("-inf"), 10**10_000),
    ids=("nan", "positive-infinity", "negative-infinity", "huge-integer"),
)
def test_backend_timeout_must_be_a_finite_plain_number(tmp_path, timeout):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")

    def must_not_launch(_backend):
        pytest.fail("invalid timeout launched a worker")

    dispatcher = BackendDispatcher(command_factory=must_not_launch)
    try:
        with pytest.raises(ValueError, match="finite"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=timeout,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


class UnexpectedBackendBaseException(BaseException):
    pass


@pytest.mark.parametrize(
    "failure_type",
    (KeyboardInterrupt, SystemExit, UnexpectedBackendBaseException),
)
def test_base_exception_after_spawn_retires_worker_and_reader(
    tmp_path, protocol_worker, monkeypatch, failure_type
):
    import d2md._backend_process as backend_process

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = backend_process.BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "timeout",
        ]
    )
    original_get = backend_process.queue.Queue.get

    def interrupt_get(self, *args, **kwargs):
        del self, args, kwargs
        raise failure_type("interrupted while waiting")

    monkeypatch.setattr(backend_process.queue.Queue, "get", interrupt_get)
    try:
        with pytest.raises(failure_type):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
        monkeypatch.setattr(backend_process.queue.Queue, "get", original_get)
        assert not any(
            thread.is_alive()
            and thread.name.startswith("d2md-markitdown-response")
            for thread in threading.enumerate()
        )
    finally:
        dispatcher.close()


@pytest.mark.parametrize(
    "failure_type",
    (KeyboardInterrupt, SystemExit, UnexpectedBackendBaseException),
)
def test_reader_base_exception_is_preserved_after_worker_cleanup(
    tmp_path, monkeypatch, failure_type
):
    import d2md._backend_process as backend_process

    class WritableInput:
        def write(self, payload):
            return len(payload)

        def flush(self):
            return None

        def close(self):
            return None

    class FailingOutput:
        def readline(self, _limit):
            raise failure_type("reader interrupted")

        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.pid = 91_002
            self.stdin = WritableInput()
            self.stdout = FailingOutput()
            self.stderr = None
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        kill = terminate

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(
        backend_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )
    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = backend_process.BackendDispatcher()
    try:
        with pytest.raises(failure_type):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_unexpected_response_processing_error_retires_worker(
    tmp_path, protocol_worker, monkeypatch
):
    import d2md._backend_process as backend_process

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = backend_process.BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "success",
        ]
    )
    monkeypatch.setattr(
        backend_process,
        "_decoded_frame",
        lambda _frame: (_ for _ in ()).throw(RuntimeError("unexpected parser bug")),
    )
    try:
        with pytest.raises(ConversionError, match="backend protocol failure"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
        assert dispatcher.worker_pid("markitdown") is None
    finally:
        dispatcher.close()


def test_default_worker_command_is_an_independent_interpreter():
    from d2md._backend_process import _default_command

    assert _default_command("docling") == [
        sys.executable,
        "-I",
        "-u",
        "-m",
        "d2md._backend_process",
        "--worker",
        "docling",
    ]


def test_control_frame_write_all_handles_short_writes():
    from d2md._backend_process import _write_all_bytes

    class ShortWriter:
        def __init__(self):
            self.buffer = bytearray()
            self.flushes = 0

        def write(self, payload):
            chunk = bytes(payload[:3])
            self.buffer.extend(chunk)
            return len(chunk)

        def flush(self):
            self.flushes += 1

    writer = ShortWriter()
    _write_all_bytes(writer, b"bounded control frame\n")

    assert bytes(writer.buffer) == b"bounded control frame\n"
    assert writer.flushes == 1


def test_blocked_parent_request_write_obeys_deadline_and_leaves_no_threads(
    tmp_path, monkeypatch
):
    import d2md._backend_process as backend_process

    released = threading.Event()
    write_started = threading.Event()

    class BlockingInput:
        def write(self, _payload):
            write_started.set()
            released.wait(timeout=10)
            raise BrokenPipeError("worker pipe closed")

        def flush(self):
            return None

        def close(self):
            released.set()

    class BlockingOutput:
        def readline(self, _limit):
            released.wait(timeout=10)
            return b""

        def close(self):
            released.set()

    class FakeProcess:
        def __init__(self):
            self.pid = 91_001
            self.stdin = BlockingInput()
            self.stdout = BlockingOutput()
            self.stderr = None
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15
            released.set()

        def kill(self):
            self.returncode = -9
            released.set()

        def wait(self, timeout=None):
            if self.returncode is None:
                raise backend_process.subprocess.TimeoutExpired("fake", timeout)
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(
        backend_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )
    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = backend_process.BackendDispatcher()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                dispatcher.run,
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=0.05,
            )
            try:
                failure = future.exception(timeout=1)
            except FutureTimeoutError:
                released.set()
                future.exception(timeout=1)
                pytest.fail("blocked request write escaped the backend deadline")

        assert isinstance(failure, ConversionError)
        assert "timed out" in str(failure)
        assert write_started.is_set()
        assert dispatcher.worker_pid("markitdown") is None
        assert not any(
            thread.is_alive()
            and thread.name.startswith(
                ("d2md-markitdown-request", "d2md-markitdown-response")
            )
            for thread in threading.enumerate()
        )
    finally:
        released.set()
        dispatcher.close()


def test_default_worker_ignores_a_hostile_cwd_shadow_package(
    tmp_path, monkeypatch
):
    from d2md._backend_process import BackendDispatcher

    hostile = tmp_path / "hostile"
    shadow = hostile / "d2md"
    shadow.mkdir(parents=True)
    sentinel = tmp_path / "shadow-imported"
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "_backend_process.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('shadowed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "real.txt"
    body = "real isolated MarkItDown output\n"
    source.write_text(body, encoding="utf-8")
    monkeypatch.chdir(hostile)

    dispatcher = BackendDispatcher()
    try:
        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=len(body),
            timeout_seconds=10,
        ) == body
        assert not sentinel.exists()
    finally:
        dispatcher.close()


def test_one_backend_family_failure_does_not_replace_the_other(
    tmp_path, protocol_worker
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    launches = []

    def command(backend):
        behavior = "extra" if backend == "markitdown" else "success"
        launches.append((backend, behavior))
        return [sys.executable, "-u", str(protocol_worker), behavior]

    dispatcher = BackendDispatcher(command_factory=command)
    try:
        assert dispatcher.run(
            "docling",
            source,
            max_output_chars=100,
            timeout_seconds=2,
        ) == "healthy backend output with enough text"
        docling_pid = dispatcher.worker_pid("docling")

        with pytest.raises(ConversionError, match="invalid success response"):
            dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )

        assert dispatcher.worker_pid("docling") == docling_pid
        assert dispatcher.run(
            "docling",
            source,
            max_output_chars=100,
            timeout_seconds=2,
        ) == "healthy backend output with enough text"
        assert launches == [("docling", "success"), ("markitdown", "extra")]
    finally:
        dispatcher.close()


def test_concurrent_calls_are_serialized_through_one_worker(
    tmp_path, protocol_worker
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    launches = []

    def command(_backend):
        launches.append("success")
        return [sys.executable, "-u", str(protocol_worker), "success"]

    dispatcher = BackendDispatcher(command_factory=command)
    try:
        def convert_once(_index):
            return dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(convert_once, range(8)))

        assert results == ["healthy backend output with enough text"] * 8
        assert launches == ["success"]
        assert dispatcher.worker_pid("markitdown") is not None
    finally:
        dispatcher.close()

    assert dispatcher.worker_pid("markitdown") is None


@pytest.mark.skipif(
    not hasattr(os, "fork") or os.name == "nt",
    reason="requires POSIX fork semantics",
)
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_fork_while_dispatcher_lock_is_held_resets_child_state(
    tmp_path, protocol_worker
):
    from d2md._backend_process import BackendDispatcher

    source = tmp_path / "input.bin"
    source.write_bytes(b"verified input")
    dispatcher = BackendDispatcher(
        command_factory=lambda _backend: [
            sys.executable,
            "-u",
            str(protocol_worker),
            "success",
        ]
    )
    expected = "healthy backend output with enough text"
    assert dispatcher.run(
        "markitdown",
        source,
        max_output_chars=100,
        timeout_seconds=2,
    ) == expected
    parent_worker_pid = dispatcher.worker_pid("markitdown")

    held = threading.Event()
    release = threading.Event()

    def hold_dispatcher_lock():
        with dispatcher._lock:
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_dispatcher_lock)
    holder.start()
    assert held.wait(timeout=2)

    child_pid = os.fork()
    if child_pid == 0:
        signal.alarm(4)
        try:
            converted = dispatcher.run(
                "markitdown",
                source,
                max_output_chars=100,
                timeout_seconds=2,
            )
            dispatcher.close()
            os._exit(0 if converted == expected else 2)
        except BaseException:
            os._exit(3)

    waited = False
    status = 0
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if waited_pid == child_pid:
                waited = True
                break
            time.sleep(0.02)
        if not waited:
            os.kill(child_pid, signal.SIGKILL)
            _waited_pid, status = os.waitpid(child_pid, 0)
            waited = True
    finally:
        release.set()
        holder.join(timeout=2)

    try:
        assert waited
        assert os.WIFEXITED(status), status
        assert os.WEXITSTATUS(status) == 0

        # The child closed only its inherited handles; it never signaled the
        # parent-owned process, which remains warm and usable here.
        assert dispatcher.worker_pid("markitdown") == parent_worker_pid
        assert dispatcher.run(
            "markitdown",
            source,
            max_output_chars=100,
            timeout_seconds=2,
        ) == expected
        assert dispatcher.worker_pid("markitdown") == parent_worker_pid
    finally:
        dispatcher.close()
