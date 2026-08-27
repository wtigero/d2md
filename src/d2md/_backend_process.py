"""Bounded reusable subprocesses for optional heavy conversion backends."""

from __future__ import annotations

import argparse
import atexit
import codecs
import json
import math
import os
from pathlib import Path
import queue
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from typing import BinaryIO


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024
_RESULT_CHUNK_CHARS = 16 * 1024
_RESULT_CHUNK_BYTES = 64 * 1024
_BACKENDS = frozenset({"markitdown", "docling"})
_VALIDATED_FORMATS = frozenset({"pdf", "image"})
_DEVICES = frozenset({"auto", "cpu", "cuda", "mps", "xpu"})
_SCRIPTS = frozenset(
    {"thai", "japanese", "korean", "cyrillic", "arabic", "latin", "chinese"}
)

_DISPATCHERS: weakref.WeakSet[object] = weakref.WeakSet()


def _reset_dispatchers_after_fork() -> None:
    for dispatcher in tuple(_DISPATCHERS):
        dispatcher._after_fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_dispatchers_after_fork)


class _ProtocolError(RuntimeError):
    pass


def _conversion_error(message: str) -> Exception:
    from .errors import ConversionError

    return ConversionError(message)


def _default_command(backend: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-u",
        "-m",
        "d2md._backend_process",
        "--worker",
        backend,
    ]


def _encoded_frame(payload: dict[str, object]) -> bytes:
    try:
        frame = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
    except (TypeError, ValueError) as exc:
        raise _ProtocolError("backend protocol frame is not serializable") from exc
    if len(frame) > MAX_FRAME_BYTES:
        raise _ProtocolError("backend protocol frame is too large")
    return frame


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decoded_frame(frame: bytes) -> dict[str, object]:
    if not frame:
        raise _ProtocolError("backend worker closed its protocol pipe")
    if len(frame) > MAX_FRAME_BYTES:
        raise _ProtocolError("backend response frame is too large")
    if not frame.endswith(b"\n"):
        raise _ProtocolError("backend response frame is incomplete")
    try:
        text = frame[:-1].decode("utf-8", errors="strict")
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise _ProtocolError("backend response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise _ProtocolError("backend response must be a JSON object")
    return value


def _readline(stream: BinaryIO, output: queue.Queue[object]) -> None:
    try:
        output.put(stream.readline(MAX_FRAME_BYTES + 1))
    except BaseException as exc:
        output.put(exc)


def _write_frame(
    stream: BinaryIO,
    frame: bytes,
    output: queue.Queue[object],
) -> None:
    try:
        _write_all_bytes(stream, frame)
        output.put(None)
    except BaseException as exc:
        output.put(exc)


def _write_all_bytes(stream: BinaryIO, frame: bytes) -> None:
    view = memoryview(frame)
    offset = 0
    while offset < len(view):
        written = stream.write(view[offset:])
        if not _is_plain_int(written) or written <= 0:
            raise OSError("backend protocol pipe made no write progress")
        offset += written
    stream.flush()


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_limit(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not _is_plain_int(value) or value < 0:
        raise _ProtocolError(f"invalid {name}")
    return value


def _validate_timeout(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float}:
        raise ValueError("timeout_seconds must be a finite non-negative number or None")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "timeout_seconds must be a finite non-negative number or None"
        ) from exc
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or normalized > threading.TIMEOUT_MAX
    ):
        raise ValueError(
            "timeout_seconds must be a finite non-negative number or None"
        )
    return normalized


def _validate_backend_options(
    *,
    validated_formats: frozenset[str],
    script: str | None,
    force_ocr: bool,
    device: str,
    ocr_enabled: bool,
) -> None:
    if (
        not isinstance(validated_formats, frozenset)
        or not validated_formats.issubset(_VALIDATED_FORMATS)
    ):
        raise ValueError("validated_formats contains an unsupported format")
    if script is not None and script not in _SCRIPTS:
        raise ValueError("script contains an unsupported value")
    if type(force_ocr) is not bool or type(ocr_enabled) is not bool:
        raise ValueError("OCR flags must be booleans")
    if device not in _DEVICES:
        raise ValueError("device contains an unsupported value")


def _verified_regular_path(path: Path, *, label: str) -> tuple[Path, os.stat_result]:
    if not path.is_absolute():
        raise _ProtocolError(f"{label} path must be absolute")
    try:
        details = path.lstat()
    except OSError as exc:
        raise _ProtocolError(f"cannot inspect {label} path") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise _ProtocolError(f"{label} path must be a regular file")
    return path, details


def _private_result_target(value: object) -> Path:
    if not isinstance(value, str):
        raise _ProtocolError("result path must be a string")
    path = Path(value)
    if not path.is_absolute() or path.exists():
        raise _ProtocolError("result path is not a fresh absolute target")
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise _ProtocolError("cannot inspect result directory") from exc
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise _ProtocolError("result directory is not private")
    if os.name != "nt" and parent.st_mode & 0o077:
        raise _ProtocolError("result directory is not private")
    if hasattr(os, "getuid") and parent.st_uid != os.getuid():
        raise _ProtocolError("result directory has the wrong owner")
    return path


class _Worker:
    def __init__(
        self,
        backend: str,
        command_factory: Callable[[str], Sequence[str]],
    ) -> None:
        self.backend = backend
        self._command_factory = command_factory
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()

    @property
    def pid(self) -> int | None:
        process = self._process
        if process is None or process.poll() is not None:
            return None
        return process.pid

    def _after_fork_child(self) -> None:
        process = self._process
        self._process = None
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()
        if process is None:
            return
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is None:
                continue
            try:
                descriptor = stream.fileno()
            except (OSError, ValueError):
                descriptor = -1
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            setattr(process, name, None)
        # This Popen belongs to the parent and cannot be waited on by the
        # forked child. Mark only the inherited Python object as settled so
        # its destructor never polls or signals the parent-owned process.
        process.returncode = 0

    def _spawn(self) -> subprocess.Popen[bytes]:
        command = list(self._command_factory(self.backend))
        if not command:
            raise _ProtocolError("backend worker command is empty")
        environment = os.environ.copy()
        environment["ORT_DISABLE_TELEMETRY"] = "1"
        try:
            return subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
                env=environment,
            )
        except OSError as exc:
            raise _ProtocolError("cannot start backend worker") from exc

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        if process is not None:
            self._discard_streams(process)
            try:
                process.wait(timeout=0)
            except (subprocess.TimeoutExpired, OSError):
                pass
        process = self._spawn()
        self._process = process
        return process

    @staticmethod
    def _discard_streams(process: subprocess.Popen[bytes]) -> None:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _retire(self, process: subprocess.Popen[bytes]) -> None:
        if self._process is process:
            self._process = None
        try:
            if process.poll() is not None:
                process.wait(timeout=0)
                return
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        except OSError:
            pass
        finally:
            self._discard_streams(process)

    def close(self) -> None:
        if os.getpid() != self._owner_pid:
            self._after_fork_child()
            return
        with self._lock:
            process = self._process
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._retire(process)
            else:
                self._process = None
                self._discard_streams(process)

    def run(
        self,
        path: Path,
        *,
        max_output_chars: int | None,
        timeout_seconds: int | float | None,
        validated_formats: frozenset[str],
        script: str | None,
        force_ocr: bool,
        device: str,
        ocr_enabled: bool,
    ) -> str:
        if os.getpid() != self._owner_pid:
            self._after_fork_child()
        with self._lock:
            started = time.monotonic()
            try:
                process = self._ensure_process()
            except _ProtocolError as exc:
                raise _conversion_error(str(exc)) from exc
            request_id = secrets.token_hex(16)
            with tempfile.TemporaryDirectory(prefix="d2md-result-") as directory:
                result_directory = Path(directory)
                if os.name != "nt":
                    result_directory.chmod(0o700)
                result_path = result_directory / "result.md"
                request = {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "backend": self.backend,
                    "path": os.fspath(path),
                    "result_path": os.fspath(result_path),
                    "max_output_chars": max_output_chars,
                    "validated_formats": sorted(validated_formats),
                    "script": script,
                    "force_ocr": force_ocr,
                    "device": device,
                    "ocr_enabled": ocr_enabled,
                }
                deadline = (
                    None
                    if timeout_seconds is None
                    else started + timeout_seconds
                )
                reader: threading.Thread | None = None
                writer: threading.Thread | None = None
                try:
                    frame = _encoded_frame(request)
                    if process.stdin is None or process.stdout is None:
                        raise _ProtocolError("backend worker pipes are unavailable")
                    writes: queue.Queue[object] = queue.Queue(maxsize=1)
                    responses: queue.Queue[object] = queue.Queue(maxsize=1)
                    reader = threading.Thread(
                        target=_readline,
                        args=(process.stdout, responses),
                        daemon=True,
                        name=f"d2md-{self.backend}-response",
                    )
                    writer = threading.Thread(
                        target=_write_frame,
                        args=(process.stdin, frame, writes),
                        daemon=True,
                        name=f"d2md-{self.backend}-request",
                    )
                    reader.start()
                    writer.start()

                    remaining = (
                        None
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                    try:
                        written = writes.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise _ProtocolError("backend conversion timed out") from exc
                    if isinstance(written, BaseException):
                        raise written

                    remaining = (
                        None
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                    try:
                        received = responses.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise _ProtocolError("backend conversion timed out") from exc
                    if isinstance(received, BaseException):
                        if isinstance(received, (KeyboardInterrupt, SystemExit)):
                            raise received
                        if not isinstance(received, Exception):
                            raise received
                        raise _ProtocolError(
                            "cannot read backend response"
                        ) from received
                    response = _decoded_frame(received)
                    return self._accept_response(
                        response,
                        request_id=request_id,
                        expected_result=result_path,
                        max_output_chars=max_output_chars,
                    )
                except _ProtocolError as exc:
                    self._retire(process)
                    raise _conversion_error(str(exc)) from exc
                except (BrokenPipeError, OSError) as exc:
                    self._retire(process)
                    raise _conversion_error("backend worker crashed") from exc
                except (KeyboardInterrupt, SystemExit):
                    self._retire(process)
                    raise
                except Exception as exc:
                    self._retire(process)
                    raise _conversion_error("backend protocol failure") from exc
                except BaseException:
                    self._retire(process)
                    raise
                finally:
                    if writer is not None and writer.ident is not None:
                        writer.join(timeout=1)
                    if reader is not None and reader.ident is not None:
                        reader.join(timeout=1)

    def _accept_response(
        self,
        response: dict[str, object],
        *,
        request_id: str,
        expected_result: Path,
        max_output_chars: int | None,
    ) -> str:
        if response.get("v") != PROTOCOL_VERSION:
            raise _ProtocolError("backend response has an unsupported version")
        if response.get("id") != request_id:
            raise _ProtocolError("backend response has a stale request ID")
        status_value = response.get("status")
        if status_value == "error":
            if set(response) != {"v", "id", "status", "code", "message"}:
                raise _ProtocolError("backend returned an invalid error response")
            code = response.get("code")
            if not isinstance(code, str) or code not in {
                "output_limit",
                "backend_failure",
            }:
                raise _ProtocolError("backend returned an invalid error response")
            message = response.get("message")
            if not isinstance(message, str) or not message or len(message) > 2_048:
                raise _ProtocolError("backend returned an invalid error response")
            expected_message = {
                "output_limit": "backend output limit exceeded",
                "backend_failure": f"{self.backend} backend failed",
            }[code]
            if message != expected_message:
                raise _ProtocolError("backend returned an invalid error response")
            raise _ProtocolError(expected_message)
        if status_value != "ok":
            raise _ProtocolError("backend response has an invalid status")
        if set(response) != {
            "v",
            "id",
            "status",
            "result_path",
            "chars",
            "bytes",
        }:
            raise _ProtocolError("backend returned an invalid success response")
        if response.get("result_path") != os.fspath(expected_result):
            raise _ProtocolError("backend response named an unexpected result path")
        byte_count = response.get("bytes")
        char_count = response.get("chars")
        if (
            not _is_plain_int(byte_count)
            or byte_count < 0
            or not _is_plain_int(char_count)
            or char_count < 0
        ):
            raise _ProtocolError("backend response has invalid result counts")
        try:
            expected_details = expected_result.lstat()
        except OSError as exc:
            raise _ProtocolError("backend result is missing") from exc
        if stat.S_ISLNK(expected_details.st_mode) or not stat.S_ISREG(
            expected_details.st_mode
        ):
            raise _ProtocolError("backend result is not a regular file")
        if expected_details.st_size != byte_count:
            raise _ProtocolError("backend result byte count does not match")
        if max_output_chars is not None and expected_details.st_size > 4 * max_output_chars:
            raise _ProtocolError("backend result exceeds its byte bound")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(expected_result, flags)
        except OSError as exc:
            raise _ProtocolError("cannot open backend result safely") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (expected_details.st_dev, expected_details.st_ino)
            ):
                raise _ProtocolError("backend result changed before it was read")
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            pieces: list[str] = []
            seen_bytes = 0
            seen_chars = 0
            while chunk := os.read(descriptor, _RESULT_CHUNK_BYTES):
                seen_bytes += len(chunk)
                text = decoder.decode(chunk, final=False)
                seen_chars += len(text)
                if max_output_chars is not None and seen_chars > max_output_chars:
                    raise _ProtocolError("backend result exceeds its character bound")
                pieces.append(text)
            tail = decoder.decode(b"", final=True)
            seen_chars += len(tail)
            if max_output_chars is not None and seen_chars > max_output_chars:
                raise _ProtocolError("backend result exceeds its character bound")
            pieces.append(tail)
        except UnicodeDecodeError as exc:
            raise _ProtocolError("backend result is not valid UTF-8") from exc
        finally:
            os.close(descriptor)
        if seen_bytes != byte_count or seen_chars != char_count:
            raise _ProtocolError("backend result counts do not match")
        return "".join(pieces)


class BackendDispatcher:
    """Serialize requests through one reusable worker per backend family."""

    def __init__(
        self,
        command_factory: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._command_factory = command_factory or _default_command
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()
        _DISPATCHERS.add(self)

    def _after_fork_child(self) -> None:
        inherited = tuple(self._workers.values())
        self._workers = {}
        self._lock = threading.Lock()
        self._owner_pid = os.getpid()
        for worker in inherited:
            worker._after_fork_child()

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._after_fork_child()

    def _worker(self, backend: str) -> _Worker:
        if backend not in _BACKENDS:
            raise ValueError(f"unsupported isolated backend: {backend}")
        self._check_owner()
        with self._lock:
            worker = self._workers.get(backend)
            if worker is None:
                worker = _Worker(backend, self._command_factory)
                self._workers[backend] = worker
            return worker

    def worker_pid(self, backend: str) -> int | None:
        self._check_owner()
        with self._lock:
            worker = self._workers.get(backend)
            return None if worker is None else worker.pid

    def run(
        self,
        backend: str,
        path: Path,
        *,
        max_output_chars: int | None,
        timeout_seconds: int | float | None,
        validated_formats: frozenset[str] = frozenset(),
        script: str | None = None,
        force_ocr: bool = False,
        device: str = "auto",
        ocr_enabled: bool = False,
    ) -> str:
        max_output_chars = _validate_limit(max_output_chars, "output limit")
        timeout_seconds = _validate_timeout(timeout_seconds)
        _validate_backend_options(
            validated_formats=validated_formats,
            script=script,
            force_ocr=force_ocr,
            device=device,
            ocr_enabled=ocr_enabled,
        )
        verified_path, _details = _verified_regular_path(Path(path), label="input")
        return self._worker(backend).run(
            verified_path,
            max_output_chars=max_output_chars,
            timeout_seconds=timeout_seconds,
            validated_formats=validated_formats,
            script=script,
            force_ocr=force_ocr,
            device=device,
            ocr_enabled=ocr_enabled,
        )

    def close(self) -> None:
        if os.getpid() != self._owner_pid:
            self._after_fork_child()
            return
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()


_DISPATCHER = BackendDispatcher()
atexit.register(_DISPATCHER.close)


def run_isolated_backend(
    backend: str,
    path: Path,
    *,
    max_output_chars: int | None,
    timeout_seconds: int | float | None,
    validated_formats: frozenset[str] = frozenset(),
    script: str | None = None,
    force_ocr: bool = False,
    device: str = "auto",
    ocr_enabled: bool = False,
) -> str:
    return _DISPATCHER.run(
        backend,
        path,
        max_output_chars=max_output_chars,
        timeout_seconds=timeout_seconds,
        validated_formats=validated_formats,
        script=script,
        force_ocr=force_ocr,
        device=device,
        ocr_enabled=ocr_enabled,
    )


def close() -> None:
    _DISPATCHER.close()


def _validate_request(request: dict[str, object], family: str) -> dict[str, object]:
    expected = {
        "v",
        "id",
        "backend",
        "path",
        "result_path",
        "max_output_chars",
        "validated_formats",
        "script",
        "force_ocr",
        "device",
        "ocr_enabled",
    }
    if set(request) != expected or request.get("v") != PROTOCOL_VERSION:
        raise _ProtocolError("invalid backend request")
    request_id = request.get("id")
    if not isinstance(request_id, str) or len(request_id) != 32:
        raise _ProtocolError("invalid backend request ID")
    if request.get("backend") != family:
        raise _ProtocolError("backend request reached the wrong worker")
    path_value = request.get("path")
    if not isinstance(path_value, str):
        raise _ProtocolError("input path must be a string")
    path, _details = _verified_regular_path(Path(path_value), label="input")
    result_path = _private_result_target(request.get("result_path"))
    max_output_chars = _validate_limit(
        request.get("max_output_chars"), "output limit"
    )
    formats_value = request.get("validated_formats")
    if (
        not isinstance(formats_value, list)
        or any(not isinstance(value, str) for value in formats_value)
    ):
        raise _ProtocolError("invalid validated formats")
    formats = frozenset(formats_value)
    script = request.get("script")
    force_ocr = request.get("force_ocr")
    device = request.get("device")
    ocr_enabled = request.get("ocr_enabled")
    try:
        _validate_backend_options(
            validated_formats=formats,
            script=script if isinstance(script, str) else None,
            force_ocr=force_ocr,
            device=device,
            ocr_enabled=ocr_enabled,
        )
    except (TypeError, ValueError) as exc:
        raise _ProtocolError("invalid backend options") from exc
    if script is not None and not isinstance(script, str):
        raise _ProtocolError("invalid backend script")
    return {
        "id": request_id,
        "path": path,
        "result_path": result_path,
        "max_output_chars": max_output_chars,
        "validated_formats": formats,
        "script": script,
        "force_ocr": force_ocr,
        "device": device,
        "ocr_enabled": ocr_enabled,
    }


def _write_result(path: Path, markdown: str) -> int:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    written = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as result:
            descriptor = -1
            for offset in range(0, len(markdown), _RESULT_CHUNK_CHARS):
                chunk = markdown[offset : offset + _RESULT_CHUNK_CHARS].encode(
                    "utf-8", errors="strict"
                )
                result.write(chunk)
                written += len(chunk)
            result.flush()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return written


def _send(protocol: BinaryIO, payload: dict[str, object]) -> None:
    _write_all_bytes(protocol, _encoded_frame(payload))


def _worker_main(family: str) -> int:
    protocol_descriptor = os.dup(sys.stdout.fileno())
    devnull_descriptor = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_descriptor, 1)
    os.close(devnull_descriptor)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    protocol = os.fdopen(protocol_descriptor, "wb", buffering=0)

    from ._onnx import prepare_onnx_telemetry_opt_out

    prepare_onnx_telemetry_opt_out()
    from .convert import _via_docling, _via_markitdown

    while True:
        frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 1)
        if not frame:
            return 0
        request_id: str | None = None
        try:
            request = _decoded_frame(frame)
            request_id_value = request.get("id")
            request_id = request_id_value if isinstance(request_id_value, str) else None
            values = _validate_request(request, family)
        except _ProtocolError:
            return 2

        try:
            if family == "markitdown":
                markdown = _via_markitdown(
                    values["path"], values["validated_formats"]
                )
            else:
                markdown = _via_docling(
                    values["path"],
                    values["script"],
                    values["force_ocr"],
                    values["device"],
                    ocr_enabled=values["ocr_enabled"],
                )
            if not isinstance(markdown, str):
                raise TypeError("backend returned a non-text result")
            char_count = len(markdown)
            max_output_chars = values["max_output_chars"]
            if max_output_chars is not None and char_count > max_output_chars:
                _send(
                    protocol,
                    {
                        "v": PROTOCOL_VERSION,
                        "id": request_id,
                        "status": "error",
                        "code": "output_limit",
                        "message": "backend output limit exceeded",
                    },
                )
                return 1
            byte_count = _write_result(values["result_path"], markdown)
            _send(
                protocol,
                {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "status": "ok",
                    "result_path": os.fspath(values["result_path"]),
                    "chars": char_count,
                    "bytes": byte_count,
                },
            )
        except BaseException:
            _send(
                protocol,
                {
                    "v": PROTOCOL_VERSION,
                    "id": request_id,
                    "status": "error",
                    "code": "backend_failure",
                    "message": f"{family} backend failed",
                },
            )
            return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m d2md._backend_process",
        description="Internal isolated converter worker.",
    )
    parser.add_argument("--worker", choices=sorted(_BACKENDS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.worker is None:
        return 0
    return _worker_main(arguments.worker)


if __name__ == "__main__":
    raise SystemExit(main())
