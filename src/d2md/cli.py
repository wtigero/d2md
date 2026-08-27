from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext, redirect_stdout
from dataclasses import dataclass
import errno
import json
import logging
import os
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
import warnings
from pathlib import Path

# Read by transformers when docling first imports it, which happens inside a
# conversion — so an environment default is enough, and unlike the warning and
# logging suppression below it costs an importer nothing.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from . import __version__  # noqa: E402
from .capabilities import (  # noqa: E402
    ensure_docling_available,
    ensure_ocr_available,
    install_command,
)
from .convert import (  # noqa: E402
    DEFAULT_LIMITS,
    DEVICE_CHOICES,
    SUPPORTED,
    UNLIMITED_LIMITS,
    ConversionError,
    ConversionLimits,
    convert,
)


_HAS_SECURE_DIR_FD = (
    os.name != "nt"
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open, os.mkdir, os.unlink}.issubset(os.supports_dir_fd)
)


@dataclass(frozen=True)
class CollectedInput:
    """A source selected by the CLI and the regular file selected there."""

    path: Path
    identity: tuple[int, int]
    root: Path | None = None


@dataclass
class OutputDirectory:
    """An output root held by descriptor where the platform supports it."""

    path: Path
    descriptor: int | None
    identity: tuple[int, int]

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def display_text(value: object) -> str:
    """Make untrusted filenames and backend errors safe for a terminal."""
    rendered: list[str] = []
    for char in str(value):
        category = unicodedata.category(char)
        if category.startswith("C") or not char.isprintable():
            codepoint = ord(char)
            if codepoint <= 0xFF:
                rendered.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                rendered.append(f"\\u{codepoint:04x}")
            else:
                rendered.append(f"\\U{codepoint:08x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def _has_terminal_controls(value: str) -> bool:
    """Return whether Markdown contains controls unsafe to write to a TTY."""
    for index, char in enumerate(value):
        if char in {"\n", "\t"}:
            continue
        if char == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
            continue
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            return True
    return False


def _stdout_isatty() -> bool:
    """Return whether standard output is an interactive terminal."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _restore_source_name(
    error: ConversionError, snapshot: Path, source: Path
) -> ConversionError:
    """Avoid exposing a transient snapshot filename in CLI diagnostics."""
    message = str(error).replace(str(snapshot), source.name)
    return ConversionError(message.replace(snapshot.name, source.name))


def _append_collected(
    files: list[CollectedInput],
    item: CollectedInput,
    max_files: int | None,
) -> None:
    if max_files is not None and len(files) >= max_files:
        raise ConversionError(
            "file limit exceeded while collecting inputs: "
            f"maximum is {max_files:,} supported files"
        )
    files.append(item)


def collect(
    paths: list[str], max_files: int | None = DEFAULT_LIMITS.max_files
) -> tuple[list[CollectedInput], list[tuple[Path, str]]]:
    """Collect regular inputs without following directory-discovered links.

    ``max_files`` is also a discovery budget for directory entries.  Otherwise
    a directory containing millions of unsupported files could consume memory
    and time while yielding no convertible inputs at all.
    """
    files: list[CollectedInput] = []
    failures: list[tuple[Path, str]] = []
    discovered_entries = 0

    def limit_error() -> ConversionError:
        limit = f"{max_files:,}" if max_files is not None else "unlimited"
        return ConversionError(
            "file limit exceeded while collecting inputs: "
            f"maximum is {limit} supported files or discovered directory entries"
        )

    def append_failure(path: Path, reason: str) -> None:
        # A collection that contains only broken paths must not create an
        # unbounded in-memory failure report either.  ``None`` remains the
        # explicit trusted-input escape hatch used by --unsafe-unlimited.
        if max_files is not None and len(failures) >= max_files:
            raise limit_error()
        failures.append((path, reason))

    def consume_directory_entry() -> None:
        nonlocal discovered_entries
        if max_files is not None and discovered_entries >= max_files:
            raise limit_error()
        discovered_entries += 1

    for raw in paths:
        path = Path(raw).expanduser()
        try:
            details = path.lstat()
        except FileNotFoundError:
            append_failure(path, "not found")
            continue
        except OSError as exc:
            append_failure(path, f"cannot inspect input: {exc}")
            continue

        if _is_link_or_reparse(details):
            # Refusing links even when supplied explicitly keeps the library
            # and CLI from reading an attacker-swapped final path.
            append_failure(path, "symbolic link inputs are rejected")
            continue

        if stat.S_ISREG(details.st_mode):
            _append_collected(
                files, CollectedInput(path, _file_identity(details)), max_files
            )
            continue
        if not stat.S_ISDIR(details.st_mode):
            append_failure(path, "input must be a regular file or directory")
            continue

        # ``os.walk`` builds a full list of names for each directory before a
        # caller can apply a limit.  Stream entries with scandir instead, so a
        # hostile directory cannot turn an ignored extension into unbounded
        # work or an unbounded ``failures`` list.
        pending_directories = [path]
        while pending_directories:
            current_path = pending_directories.pop()
            try:
                entries = os.scandir(current_path)
            except OSError as exc:
                append_failure(
                    Path(exc.filename or current_path),
                    f"cannot scan directory: {exc}",
                )
                continue

            with entries:
                for entry in entries:
                    consume_directory_entry()
                    candidate = Path(entry.path)
                    try:
                        candidate_details = candidate.lstat()
                    except OSError as exc:
                        append_failure(candidate, f"cannot inspect input: {exc}")
                        continue

                    if _is_link_or_reparse(candidate_details):
                        append_failure(candidate, "symbolic link inputs are rejected")
                        continue
                    if stat.S_ISDIR(candidate_details.st_mode):
                        pending_directories.append(candidate)
                        continue
                    if (
                        candidate.suffix.lower() in SUPPORTED
                        and stat.S_ISREG(candidate_details.st_mode)
                    ):
                        _append_collected(
                            files,
                            CollectedInput(
                                candidate, _file_identity(candidate_details), path
                            ),
                            max_files,
                        )

    return files, failures


def _file_identity(details: os.stat_result) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _is_link_or_reparse(details: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _path_chain(path: Path) -> Iterator[Path]:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        yield current


def _assert_no_link_components(path: Path, allow_missing: bool = False) -> None:
    """Reject symlinks and Windows junction/reparse points in a full path."""
    for component in _path_chain(path):
        try:
            details = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise ConversionError(f"path component does not exist: {component}")
        except OSError as exc:
            raise ConversionError(f"cannot inspect path safely: {component}: {exc}") from exc
        if _is_link_or_reparse(details):
            raise ConversionError(f"symbolic link or reparse point is rejected: {component}")


def _secure_directory_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise ConversionError(
            "safe directory handling needs O_NOFOLLOW and O_DIRECTORY on this platform"
        )
    return os.O_RDONLY | no_follow | directory


def _open_secure_directory(path: Path, purpose: str) -> int:
    try:
        descriptor = os.open(path, _secure_directory_flags())
    except OSError as exc:
        raise ConversionError(f"cannot open {purpose} directory safely: {path}: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ConversionError(f"{purpose} path is not a directory: {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _describe_output_directory_failure(
    component: str, path: Path, exc: OSError, directory_descriptor: int
) -> str:
    """Say what a rejected component actually is, rather than echo its errno.

    O_NOFOLLOW answers a symlinked component with ELOOP, or with ENOTDIR when
    macOS sees it alongside O_DIRECTORY. Neither reads as "this is a link", so
    `-o /tmp/out` reported `Not a directory: 'tmp'` and sent the reader looking
    for a problem with a directory that was fine. The portable path already
    names the link; this makes both platforms agree.
    """
    try:
        details = os.lstat(component, dir_fd=directory_descriptor)
    except OSError:
        return f"cannot open output directory safely: {path}: {exc}"

    if _is_link_or_reparse(details):
        return (
            "symbolic link or reparse point output directory is rejected: "
            f"{component} in {path}"
        )
    if not stat.S_ISDIR(details.st_mode):
        return f"output path is not a directory: {component} in {path}"
    return f"cannot open output directory safely: {path}: {exc}"


def _lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving any symbolic links."""
    return Path(os.path.abspath(os.fspath(path)))


def _open_input_file(item: CollectedInput) -> int:
    """Open an input through no-follow descriptors, component by component."""
    if not _HAS_SECURE_DIR_FD:
        return _open_input_file_portable(item)

    absolute_path = _lexical_absolute(item.path)
    if item.root is not None:
        root = _lexical_absolute(item.root)
        try:
            absolute_path.relative_to(root)
        except ValueError as exc:
            raise ConversionError(f"input escaped its directory root: {item.path}") from exc

    components = absolute_path.parts[1:]
    if not components or any(part in {"", ".", ".."} for part in components):
        raise ConversionError(f"unsafe input path: {item.path}")

    directory_descriptor = os.open("/", _secure_directory_flags())
    try:
        for component in components[:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    _secure_directory_flags(),
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise ConversionError(
                    f"cannot open directory input safely: {item.path}: {exc}"
                ) from exc
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor

        try:
            descriptor = os.open(
                components[-1],
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise ConversionError(
                f"cannot open directory input safely: {item.path}: {exc}"
            ) from exc
    finally:
        os.close(directory_descriptor)

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ConversionError(f"directory input must be a regular file: {item.path}")
        if _file_identity(opened) != item.identity:
            raise ConversionError(
                f"input changed after it was collected: {item.path}"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_input_file_portable(item: CollectedInput) -> int:
    """Open a regular input safely on Windows and other no-dir-fd platforms.

    Component checks reject Windows junctions/reparse points. Comparing the
    pre-open pathname identity with the opened descriptor catches replacement
    of the final component between inspection and use.
    """
    absolute_path = _lexical_absolute(item.path)
    if item.root is not None:
        root = _lexical_absolute(item.root)
        try:
            absolute_path.relative_to(root)
        except ValueError as exc:
            raise ConversionError(f"input escaped its directory root: {item.path}") from exc

    _assert_no_link_components(absolute_path)
    try:
        before = absolute_path.lstat()
    except OSError as exc:
        raise ConversionError(f"cannot inspect input safely: {item.path}: {exc}") from exc
    if _is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ConversionError(f"input must be a regular file: {item.path}")

    try:
        descriptor = os.open(
            absolute_path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except OSError as exc:
        raise ConversionError(f"cannot open input safely: {item.path}: {exc}") from exc

    try:
        opened = os.fstat(descriptor)
        _assert_no_link_components(absolute_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != _file_identity(before)
        ):
            raise ConversionError(f"input changed while it was being opened: {item.path}")
        if _file_identity(opened) != item.identity:
            raise ConversionError(
                f"input changed after it was collected: {item.path}"
            )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _snapshot_input(item: CollectedInput, limits: ConversionLimits) -> Iterator[Path]:
    """Pass heavy backends a bounded private snapshot of every input.

    The snapshot removes the check/use race for direct paths as well as files
    discovered recursively in a directory.  Backends only ever receive the
    private regular file, never a caller-controlled pathname.
    """
    descriptor = _open_input_file(item)
    snapshot: Path | None = None
    try:
        try:
            size = os.fstat(descriptor).st_size
            if limits.max_input_bytes is not None and size > limits.max_input_bytes:
                raise ConversionError(
                    f"input limit exceeded: {item.path.name} is larger than "
                    f"{limits.max_input_bytes:,} bytes"
                )

            with tempfile.NamedTemporaryFile(
                prefix="d2md-input-", suffix=item.path.suffix, delete=False
            ) as copy:
                # Normalise macOS's /var temporary-directory alias before
                # handing this private snapshot to the public converter,
                # which correctly rejects caller-controlled link components.
                snapshot = Path(copy.name).resolve(strict=True)
                copied = 0
                while chunk := os.read(descriptor, 1024 * 1024):
                    copied += len(chunk)
                    if (
                        limits.max_input_bytes is not None
                        and copied > limits.max_input_bytes
                    ):
                        raise ConversionError(
                            f"input limit exceeded: {item.path.name} is larger than "
                            f"{limits.max_input_bytes:,} bytes"
                        )
                    copy.write(chunk)
        except ConversionError:
            raise
        except OSError as exc:
            raise ConversionError(
                f"cannot read directory input safely: {item.path}: {exc}"
            ) from exc

        assert snapshot is not None
        yield snapshot
    finally:
        os.close(descriptor)
        if snapshot is not None:
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass


def _open_output_directory(path: Path) -> OutputDirectory:
    """Create/open every output component without following a directory link."""
    if not _HAS_SECURE_DIR_FD:
        return _open_output_directory_portable(path)

    absolute_path = _lexical_absolute(path)
    components = absolute_path.parts[1:]
    directory_descriptor = _open_secure_directory(Path("/"), "filesystem root")
    try:
        for component in components:
            try:
                next_descriptor = os.open(
                    component,
                    _secure_directory_flags(),
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=directory_descriptor)
                except FileExistsError:
                    pass  # A concurrent creator is safe only if the next open is too.
                except OSError as exc:
                    raise ConversionError(
                        f"cannot create output directory: {path}: {exc}"
                    ) from exc
                try:
                    next_descriptor = os.open(
                        component,
                        _secure_directory_flags(),
                        dir_fd=directory_descriptor,
                    )
                except OSError as exc:
                    raise ConversionError(
                        f"cannot open output directory safely: {path}: {exc}"
                    ) from exc
            except OSError as exc:
                raise ConversionError(
                    _describe_output_directory_failure(
                        component, path, exc, directory_descriptor
                    )
                ) from exc
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
    except Exception:
        os.close(directory_descriptor)
        raise
    return OutputDirectory(
        path=absolute_path,
        descriptor=directory_descriptor,
        identity=_file_identity(os.fstat(directory_descriptor)),
    )


def _open_output_directory_portable(path: Path) -> OutputDirectory:
    absolute_path = _lexical_absolute(path)
    for component in _path_chain(absolute_path):
        try:
            details = component.lstat()
        except FileNotFoundError:
            try:
                component.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise ConversionError(
                    f"cannot create output directory: {path}: {exc}"
                ) from exc
            try:
                details = component.lstat()
            except OSError as exc:
                raise ConversionError(
                    f"cannot inspect output directory: {path}: {exc}"
                ) from exc
        except OSError as exc:
            raise ConversionError(
                f"cannot inspect output directory: {path}: {exc}"
            ) from exc

        if _is_link_or_reparse(details):
            raise ConversionError(
                f"symbolic link or reparse point output directory is rejected: {component}"
            )
        if not stat.S_ISDIR(details.st_mode):
            raise ConversionError(f"output path is not a directory: {component}")

    details = absolute_path.lstat()
    return OutputDirectory(
        path=absolute_path,
        descriptor=None,
        identity=_file_identity(details),
    )


def _assert_output_directory_unchanged(output: OutputDirectory) -> None:
    if output.descriptor is not None:
        return
    _assert_no_link_components(output.path)
    try:
        details = output.path.lstat()
    except OSError as exc:
        raise ConversionError(
            f"cannot inspect output directory safely: {output.path}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or _file_identity(details) != output.identity
    ):
        raise ConversionError(f"output directory changed during conversion: {output.path}")


def _destination_status(output: OutputDirectory, filename: str) -> str:
    _safe_output_filename(filename)
    if output.descriptor is not None:
        try:
            details = os.lstat(filename, dir_fd=output.descriptor)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise ConversionError(
                f"cannot inspect output safely: {filename}: {exc}"
            ) from exc
    else:
        _assert_output_directory_unchanged(output)
        try:
            details = (output.path / filename).lstat()
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise ConversionError(
                f"cannot inspect output safely: {filename}: {exc}"
            ) from exc
    return "symlink" if _is_link_or_reparse(details) else "existing"


def _safe_output_filename(filename: str) -> None:
    if (
        not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
    ):
        raise ConversionError(f"unsafe output filename: {filename!r}")


def _write_output(
    output: OutputDirectory, filename: str, markdown: str, force: bool
) -> bool:
    """Atomically publish output without ever following its final component.

    Returns ``False`` only when a no-force destination appeared after the
    initial check.  In that race the existing entry is intentionally untouched.
    """
    _safe_output_filename(filename)
    if output.descriptor is None:
        return _write_output_portable(output, filename, markdown, force)

    directory_descriptor = output.descriptor
    temporary_name = f".d2md-{uuid.uuid4().hex}.tmp"
    temporary_exists = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_exists = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(markdown)

        if force:
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            temporary_exists = False
            return True

        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_exists = False
        return True
    except OSError as exc:
        raise ConversionError(f"cannot write output safely: {filename}: {exc}") from exc
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass


def _write_output_portable(
    output: OutputDirectory, filename: str, markdown: str, force: bool
) -> bool:
    """Atomically publish output on Windows without following its final entry."""
    _assert_output_directory_unchanged(output)
    temporary = output.path / f".d2md-{uuid.uuid4().hex}.tmp"
    destination = output.path / filename
    temporary_exists = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        temporary_exists = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(markdown)

        _assert_output_directory_unchanged(output)
        if force:
            os.replace(temporary, destination)
            temporary_exists = False
            return True

        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            return False
        except (NotImplementedError, TypeError):
            # Not every filesystem accepts the keyword. The retry has to keep
            # the same contract, or an existing destination is reported as a
            # write failure instead of the refusal it actually is.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return False
        temporary.unlink()
        temporary_exists = False
        return True
    except OSError as exc:
        raise ConversionError(f"cannot write output safely: {filename}: {exc}") from exc
    finally:
        if temporary_exists:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def report_engines() -> int:
    """Print what this machine can actually OCR.

    Coverage is not the same everywhere — Apple Vision reads seven scripts and
    exists only on macOS — so the honest answer is machine-specific, and
    guessing it is how a scanned Thai page came back as its phone number.
    """
    from .ocr import ENGINE_SCRIPTS, available_engines, supported_scripts

    engines = available_engines()
    if not engines:
        print("no OCR engine installed", file=sys.stderr)
        print(
            f"install it with: {install_command('ocr', force=True)}",
            file=sys.stderr,
        )
        return 1

    for name in engines:
        scripts = sorted(s for s, code in ENGINE_SCRIPTS[name].items() if code)
        print(f"{display_text(name):10s} {', '.join(display_text(s) for s in scripts)}")

    print(
        "\nthis machine reads: "
        + ", ".join(display_text(s) for s in sorted(supported_scripts()))
    )
    return 0


def _capabilities_payload() -> dict[str, object]:
    """Describe optional features without treating absence as an error."""
    from . import capabilities
    from .ocr import ENGINE_SCRIPTS, available_engines, supported_scripts

    engines = available_engines()
    engine_rows = [
        {
            "name": name,
            "scripts": sorted(
                script
                for script, code in ENGINE_SCRIPTS.get(name, {}).items()
                if code
            ),
        }
        for name in engines
    ]
    return {
        "schema_version": 1,
        "ocr": {
            "installed": bool(engines),
            "engines": engine_rows,
            "install_command": install_command("ocr", force=True),
            "scripts": sorted(supported_scripts(engines)),
        },
        "docling": {
            "installed": capabilities.module_available("docling"),
            "device_choices": list(DEVICE_CHOICES),
            "install_command": install_command("docling", force=True),
        },
    }


def _print_json(payload: dict[str, object]) -> None:
    # ASCII escaping keeps bidi and other Unicode control characters from
    # changing how a raw JSON line is displayed, while a JSON parser restores
    # the original Unicode values for automation.
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def report_capabilities(*, json_output: bool) -> int:
    """Report optional local capabilities for people or automation."""
    payload = _capabilities_payload()
    if json_output:
        _print_json(payload)
        return 0

    ocr = payload["ocr"]
    assert isinstance(ocr, dict)
    engines = ocr["engines"]
    assert isinstance(engines, list)
    if not engines:
        print("OCR: not installed")
    else:
        print("OCR:")
        for row in engines:
            assert isinstance(row, dict)
            print(f"  {row['name']}: {', '.join(row['scripts'])}")

    docling = payload["docling"]
    assert isinstance(docling, dict)
    print(f"Docling: {'installed' if docling['installed'] else 'not installed'}")
    print("Docling device choices: " + ", ".join(docling["device_choices"]))
    return 0


def _run_payload(
    args: argparse.Namespace,
    *,
    converted: int = 0,
    skipped: int = 0,
    results: list[dict[str, object]] | None = None,
    errors: list[tuple[Path | None, str]] | None = None,
    notices: list[str] | None = None,
) -> dict[str, object]:
    """Build the stable machine-readable envelope for one CLI invocation."""
    error_rows = [
        {"source": str(source) if source is not None else None, "message": message}
        for source, message in errors or []
    ]
    return {
        "schema_version": 1,
        "ok": not error_rows,
        "options": {
            "device": args.device,
            "docling": args.docling,
            "language": args.lang,
            "ocr": args.ocr,
        },
        "summary": {
            "converted": converted,
            "failed": len(error_rows),
            "skipped": skipped,
        },
        "results": results or [],
        "errors": error_rows,
        "warnings": notices or [],
    }


def _configure_console_streams(
    platform: str | None = None, streams: tuple[object, ...] | None = None
) -> None:
    """Use deterministic Unicode output when Windows redirects the console."""
    if (platform or os.name) != "nt":
        return

    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _reject_ambiguous_option_paths(ap: argparse.ArgumentParser, argv: list[str]) -> None:
    """Reject existing option-shaped paths before argparse can interpret them."""
    for raw in argv:
        if raw == "--":
            return
        if raw == "-" or not raw.startswith("-"):
            continue
        try:
            os.lstat(raw)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                continue
        ap.error(
            "ambiguous option-shaped path "
            f"{display_text(raw)!r}; put options before '--' and paths after it, "
            "or prefix the path with './'"
        )


def main(argv: list[str] | None = None) -> int:
    # docling, markitdown and transformers are chatty, and none of them is
    # imported until a conversion actually starts — so suppressing here is
    # early enough, and keeps `import d2md.cli` from silencing warnings and
    # logging for a process that only wanted the library.
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)

    # Under Windows OpenSSH and some CI runners, Python otherwise selects a
    # legacy code page such as cp1252 and crashes while printing Unicode file
    # names or even the progress glyphs below after a successful conversion.
    _configure_console_streams()

    ap = argparse.ArgumentParser(
        prog="d2md",
        description="Convert documents to Markdown, offline. Thai-aware.",
        allow_abbrev=False,
    )
    ap.add_argument("paths", nargs="*", help="files or directories")
    ap.add_argument("-o", "--outdir", help="output directory (default: md-out)")
    ap.add_argument("-f", "--force", action="store_true", help="reconvert existing")
    ap.add_argument("-q", "--quiet", action="store_true", help="only report failures")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    machine_output = ap.add_mutually_exclusive_group()
    machine_output.add_argument(
        "--stdout",
        action="store_true",
        help="write one converted document to standard output instead of a file",
    )
    machine_output.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="write one machine-readable run report to standard output",
    )
    ap.add_argument(
        "--fast",
        action="store_const",
        const=True,
        default=None,
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--ocr",
        action="store_true",
        help="OCR scanned PDFs and images",
    )
    ap.add_argument(
        "--docling",
        action="store_true",
        help="use Docling for PDF/image layout, headings, and tables",
    )
    ap.add_argument(
        "--lang",
        metavar="SCRIPT",
        help="script to OCR scans in (thai, latin, japanese, chinese, korean, "
        "cyrillic, arabic); skips detection, which costs an OCR pass per script",
    )
    ap.add_argument(
        "--device",
        choices=DEVICE_CHOICES,
        default="auto",
        help="Docling inference device; requires --docling",
    )
    ap.add_argument(
        "--unsafe-unlimited",
        action="store_true",
        help="disable resource limits for trusted oversized inputs; symlink protections stay enabled",
    )
    ap.add_argument(
        "--engines",
        action="store_true",
        help="list the OCR engines available here and the scripts they read, "
        "then exit",
    )
    ap.add_argument(
        "--capabilities",
        action="store_true",
        help="show installed OCR/Docling capabilities and device choices, then exit",
    )
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    _reject_ambiguous_option_paths(ap, raw_argv)
    args = ap.parse_args(raw_argv)

    if args.capabilities:
        return report_capabilities(json_output=args.json_output)
    if args.engines:
        if args.json_output:
            return report_capabilities(json_output=True)
        return report_engines()

    if args.stdout and (args.force or args.outdir is not None):
        ap.error("--stdout does not write output files; remove --force and --outdir")

    notices: list[str] = []

    def fail(message: str, code: int, source: Path | None = None) -> int:
        if args.json_output:
            _print_json(
                _run_payload(
                    args,
                    errors=[(source, message)],
                    notices=notices,
                )
            )
        else:
            print(display_text(message), file=sys.stderr)
        return code

    if args.lang and not args.ocr:
        ap.error("--lang requires --ocr")
    if args.device != "auto" and not args.docling:
        ap.error("--device requires --docling")

    if args.lang:
        from .ocr import BLOCKS, supported_scripts

        if args.lang not in BLOCKS:
            return fail(
                f"unknown script {display_text(args.lang)!r}. "
                f"Known: {', '.join(sorted(BLOCKS))}",
                2,
            )

        # An engine is only chosen while a Docling converter is being built,
        # which happens per file and after the input has been snapshotted and
        # validated. Without this check a script no installed engine reads is
        # accepted here and then raises NoEngineFor once per file — off macOS
        # that is what `--lang thai` did to every PDF routed to Docling, born-
        # digital ones included. Ask once, up front, and say what is readable.
        readable = supported_scripts()
        if args.lang not in readable:
            if not readable:
                message = "no OCR engine available — scanned PDFs cannot be read"
            else:
                message = (
                    f"no OCR engine here reads {display_text(args.lang)!r}. "
                    "This machine reads: "
                    f"{', '.join(display_text(s) for s in sorted(readable))}. "
                    "Run 'd2md --engines' for the per-engine detail."
                )
            return fail(message, 2)

    if args.fast is not None:
        message = (
            "--fast is deprecated; PDF text extraction is now the default"
        )
        if args.json_output:
            notices.append(message)
        else:
            print(f"warning: {message}", file=sys.stderr)

    try:
        if args.ocr:
            ensure_ocr_available()
        if args.docling:
            ensure_docling_available()
    except ConversionError as exc:
        return fail(str(exc), 2)

    limits = UNLIMITED_LIMITS if args.unsafe_unlimited else DEFAULT_LIMITS
    try:
        files, failures = collect(args.paths, max_files=limits.max_files)
    except ConversionError as exc:
        return fail(f"collection failed: {exc}", 1)

    if not files:
        if args.json_output:
            errors = failures or [(None, "nothing to convert")]
            _print_json(_run_payload(args, errors=errors, notices=notices))
        else:
            print("nothing to convert", file=sys.stderr)
            for source, reason in failures:
                print(
                    f"  ✗ {display_text(source)}: {display_text(reason)}",
                    file=sys.stderr,
                )
        return 1

    if args.stdout and (len(files) != 1 or failures):
        return fail("--stdout requires exactly one input", 2)

    outdir = Path(args.outdir or "md-out").expanduser()
    output_directory: OutputDirectory | None = None
    if not args.stdout:
        try:
            output_directory = _open_output_directory(outdir)
        except ConversionError as exc:
            return fail(str(exc), 2)

    done = skipped = 0
    published_output_chars = 0
    result_rows: list[dict[str, object]] = []
    stdout_markdown = ""
    machine_quiet = args.quiet or args.json_output or args.stdout
    try:
        for index, source in enumerate(files, 1):
            filename = f"{source.path.stem}.md"
            if output_directory is not None:
                try:
                    destination = _destination_status(output_directory, filename)
                except ConversionError as exc:
                    failures.append((source.path, str(exc)))
                    if not args.json_output:
                        print(f"FAILED  {display_text(exc)}")
                    continue

                if destination == "existing" and not args.force:
                    skipped += 1
                    result_rows.append(
                        {
                            "source": str(source.path),
                            "output": str(outdir / filename),
                            "status": "skipped",
                        }
                    )
                    if not machine_quiet:
                        print(
                            f"[{index}/{len(files)}] skip  "
                            f"{display_text(source.path.name)}"
                        )
                    continue
                if destination == "symlink" and not args.force:
                    message = (
                        f"refusing to write through output symbolic link: {filename}"
                    )
                    failures.append((source.path, message))
                    if not args.json_output:
                        print(
                            f"FAILED  {display_text(message)}"
                            if args.quiet
                            else f"[{index}/{len(files)}] "
                            f"{display_text(source.path.name)} … "
                            f"FAILED  {display_text(message)}"
                        )
                    continue

            if not machine_quiet:
                print(
                    f"[{index}/{len(files)}] {display_text(source.path.name)} … ",
                    end="",
                    flush=True,
                )
            started = time.time()
            try:
                with _snapshot_input(source, limits) as input_path:
                    try:
                        backend_output = (
                            redirect_stdout(sys.stderr)
                            if args.stdout or args.json_output
                            else nullcontext()
                        )
                        with backend_output:
                            result = convert(
                                input_path,
                                fast=args.fast,
                                lang=args.lang,
                                limits=limits,
                                device=args.device,
                                ocr=args.ocr,
                                docling=args.docling,
                                # input_path is a private snapshot; preserve the
                                # caller's filename in backend and limit errors.
                                display_name=source.path.name,
                            )
                    except ConversionError as error:
                        raise _restore_source_name(
                            error, input_path, source.path
                        ) from error
                prospective_output_chars = published_output_chars + len(
                    result.markdown
                )
                if (
                    limits.max_output_chars is not None
                    and prospective_output_chars > limits.max_output_chars
                ):
                    raise ConversionError(
                        "batch output limit exceeded: publishing "
                        f"{source.path.name} would produce "
                        f"{prospective_output_chars:,} characters; maximum is "
                        f"{limits.max_output_chars:,}"
                    )
                if output_directory is not None:
                    if not _write_output(
                        output_directory, filename, result.markdown, args.force
                    ):
                        raise ConversionError(
                            "refusing to overwrite output created during conversion: "
                            f"{filename}"
                        )
                published_output_chars = prospective_output_chars
            except ConversionError as exc:
                failures.append((source.path, str(exc)))
                if not args.json_output:
                    print(
                        f"FAILED  {display_text(exc)}",
                        file=sys.stderr if args.stdout else sys.stdout,
                    )
                continue
            except Exception as exc:  # backend blew up
                message = f"{type(exc).__name__}: {exc}"
                failures.append((source.path, message))
                if not args.json_output:
                    print(
                        f"FAILED  {display_text(message)}",
                        file=sys.stderr if args.stdout else sys.stdout,
                    )
                continue

            elapsed = time.time() - started
            done += 1
            if args.stdout:
                stdout_markdown = result.markdown
            else:
                result_rows.append(
                    {
                        "source": str(source.path),
                        "output": str(outdir / filename),
                        "status": "converted",
                        "backend": result.backend,
                        "characters": len(result.markdown),
                        "seconds": round(elapsed, 6),
                    }
                )
            if not machine_quiet:
                print(
                    f"{display_text(result.backend):10s} {len(result.markdown):8,d} chars"
                    f"  {elapsed:5.1f}s"
                )
    finally:
        if output_directory is not None:
            output_directory.close()

    if args.stdout:
        if failures:
            return 1
        if _stdout_isatty() and _has_terminal_controls(stdout_markdown):
            return fail(
                "refusing to write Markdown containing terminal control "
                "characters to a TTY; redirect stdout to preserve raw output",
                1,
            )
        sys.stdout.write(stdout_markdown)
        return 0

    if args.json_output:
        _print_json(
            _run_payload(
                args,
                converted=done,
                skipped=skipped,
                results=result_rows,
                errors=failures,
                notices=notices,
            )
        )
        return 1 if failures else 0

    print(
        f"\n{done} converted · {skipped} skipped · {len(failures)} failed → "
        f"{display_text(outdir)}/"
    )
    for source, reason in failures:
        print(f"  ✗ {display_text(source)}: {display_text(reason)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
