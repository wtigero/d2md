import importlib
import io
import os
import stat
import struct
import sys
import types
import zipfile

import pytest

from d2md import cli
from d2md.convert import ConversionError, ConversionLimits, convert


convert_module = importlib.import_module("d2md.convert")


BODY = "A document with enough text to pass the conversion output guard."


def add_zip64_end_records(
    payload,
    *,
    zip64_entries,
    classic_entries,
    classic_directory_is_zip64=False,
    extensible_data=b"",
    prefix=b"",
):
    end = payload.rfind(b"PK\x05\x06")
    central_directory_size = struct.unpack_from("<L", payload, end + 12)[0]
    central_directory_offset = struct.unpack_from("<L", payload, end + 16)[0]
    absolute_directory_offset = central_directory_offset + len(prefix)
    zip64_end = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44 + len(extensible_data),
        45,
        45,
        0,
        0,
        zip64_entries,
        zip64_entries,
        central_directory_size,
        absolute_directory_offset,
    ) + extensible_data
    zip64_locator = struct.pack(
        "<4sLQL", b"PK\x06\x07", 0, end + len(prefix), 1
    )
    classic_end = bytearray(payload[end:])
    struct.pack_into("<H", classic_end, 8, classic_entries)
    struct.pack_into("<H", classic_end, 10, classic_entries)
    if classic_directory_is_zip64:
        struct.pack_into("<L", classic_end, 12, 0xFFFFFFFF)
        struct.pack_into("<L", classic_end, 16, 0xFFFFFFFF)
    elif prefix:
        struct.pack_into("<L", classic_end, 16, absolute_directory_offset)
    return prefix + payload[:end] + zip64_end + zip64_locator + classic_end


def add_zip64_locator_without_record(payload):
    end = payload.rfind(b"PK\x05\x06")
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    return payload[:end] + locator + payload[end:]


def make_symlink_or_skip(link, target, target_is_directory=False):
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable for this user: {exc}")


def test_directory_conversion_rejects_symlinked_input(tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text(BODY, encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_symlink_or_skip(inbox / "linked.txt", outside)
    outdir = tmp_path / "out"

    assert cli.main([str(inbox), "-o", str(outdir)]) == 1
    assert not (outdir / "linked.md").exists()
    captured = capsys.readouterr()
    assert "symbolic link" in (captured.out + captured.err)


def test_direct_symlink_input_is_rejected(tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text(BODY, encoding="utf-8")
    linked = tmp_path / "linked.txt"
    make_symlink_or_skip(linked, outside)

    assert cli.main([str(linked), "-o", str(tmp_path / "out")]) == 1
    captured = capsys.readouterr()
    assert "symbolic link" in (captured.out + captured.err)


def test_directory_conversion_keeps_regular_input_working(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "ordinary.txt").write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"

    assert cli.main([str(inbox), "-o", str(outdir)]) == 0
    assert (outdir / "ordinary.md").read_text(encoding="utf-8") == BODY


def test_directory_collection_refreshes_incomplete_direntry_identity(
    tmp_path, monkeypatch
):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "ordinary.txt"
    source.write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"
    real_scandir = os.scandir

    class IncompleteIdentityEntry:
        def __init__(self, entry):
            self._entry = entry
            self.path = entry.path

        def stat(self, *, follow_symlinks=True):
            details = self._entry.stat(follow_symlinks=follow_symlinks)
            values = list(details)
            values[stat.ST_DEV] = 0
            values[stat.ST_INO] = 0
            return os.stat_result(values)

    class IncompleteIdentityScandir:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._entries.__exit__(*args)

        def __iter__(self):
            return (IncompleteIdentityEntry(entry) for entry in self._entries)

    monkeypatch.setattr(cli.os, "scandir", IncompleteIdentityScandir)
    monkeypatch.setattr(cli, "_HAS_SECURE_DIR_FD", False)

    assert cli.main([str(inbox), "-o", str(outdir)]) == 0
    assert (outdir / "ordinary.md").read_text(encoding="utf-8") == BODY


def test_cli_rejects_regular_input_replaced_after_collection(
    tmp_path, monkeypatch, capsys
):
    first = tmp_path / "first.txt"
    selected = tmp_path / "selected.txt"
    replacement = tmp_path / "replacement.txt"
    first.write_text("first approved document", encoding="utf-8")
    selected.write_text("second approved document", encoding="utf-8")
    replacement.write_text("attacker replacement document", encoding="utf-8")
    outdir = tmp_path / "out"
    conversions = 0

    def replace_second_while_converting_first(_path, **_kwargs):
        nonlocal conversions
        conversions += 1
        if conversions == 1:
            os.replace(replacement, selected)
        return types.SimpleNamespace(markdown=BODY, backend="plain")

    monkeypatch.setattr(cli, "convert", replace_second_while_converting_first)

    assert cli.main([str(first), str(selected), "-o", str(outdir)]) == 1
    assert conversions == 1
    assert (outdir / "first.md").read_text(encoding="utf-8") == BODY
    assert not (outdir / "selected.md").exists()
    assert "input changed after it was collected" in capsys.readouterr().out


def test_portable_cli_rejects_regular_input_replaced_after_collection(
    tmp_path, monkeypatch, capsys
):
    first = tmp_path / "first.txt"
    selected = tmp_path / "selected.txt"
    replacement = tmp_path / "replacement.txt"
    first.write_text("first approved document", encoding="utf-8")
    selected.write_text("second approved document", encoding="utf-8")
    replacement.write_text("attacker replacement document", encoding="utf-8")
    outdir = tmp_path / "out"
    conversions = 0

    def replace_second_while_converting_first(_path, **_kwargs):
        nonlocal conversions
        conversions += 1
        if conversions == 1:
            os.replace(replacement, selected)
        return types.SimpleNamespace(markdown=BODY, backend="plain")

    monkeypatch.setattr(cli, "_HAS_SECURE_DIR_FD", False)
    monkeypatch.setattr(cli, "convert", replace_second_while_converting_first)

    assert cli.main([str(first), str(selected), "-o", str(outdir)]) == 1
    assert conversions == 1
    assert (outdir / "first.md").read_text(encoding="utf-8") == BODY
    assert not (outdir / "selected.md").exists()
    assert "input changed after it was collected" in capsys.readouterr().out


def test_regular_conversion_works_without_posix_directory_descriptors(
    tmp_path, monkeypatch
):
    source = tmp_path / "portable.txt"
    source.write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"
    monkeypatch.setattr(cli, "_HAS_SECURE_DIR_FD", False)

    assert cli.main([str(source), "-o", str(outdir)]) == 0
    assert (outdir / "portable.md").read_text(encoding="utf-8") == BODY


def test_windows_redirected_console_is_reconfigured_for_unicode():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    cli._configure_console_streams(platform="nt", streams=(stream,))
    stream.write("converted → ภาษาไทย")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "converted → ภาษาไทย"


def test_force_replaces_an_output_symlink_without_touching_its_target(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("keep this content", encoding="utf-8")
    destination = outdir / "report.md"
    make_symlink_or_skip(destination, protected)

    assert cli.main([str(source), "-o", str(outdir), "--force"]) == 0
    assert protected.read_text(encoding="utf-8") == "keep this content"
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == BODY


def test_portable_output_mode_replaces_link_not_target(tmp_path, monkeypatch):
    source = tmp_path / "portable.txt"
    source.write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    protected = tmp_path / "protected.txt"
    protected.write_text("keep this content", encoding="utf-8")
    destination = outdir / "portable.md"
    make_symlink_or_skip(destination, protected)
    monkeypatch.setattr(cli, "_HAS_SECURE_DIR_FD", False)

    assert cli.main([str(source), "-o", str(outdir), "--force"]) == 0
    assert protected.read_text(encoding="utf-8") == "keep this content"
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == BODY


def test_portable_write_refuses_an_existing_destination_when_link_rejects_the_kwarg(
    tmp_path, monkeypatch
):
    """`os.link(..., follow_symlinks=False)` is not accepted on every filesystem.

    The retry without the keyword has to keep the same contract as the first
    attempt: an existing destination is a refusal that leaves the entry
    untouched, not a write failure.
    """
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "report.md").write_text("keep this content", encoding="utf-8")
    output = cli.OutputDirectory(
        path=outdir, descriptor=None, identity=cli._file_identity(outdir.lstat())
    )

    real_link = os.link

    def link_without_follow_symlinks(source, destination, **kwargs):
        if "follow_symlinks" in kwargs:
            raise TypeError("follow_symlinks is unavailable here")
        return real_link(source, destination)

    monkeypatch.setattr(os, "link", link_without_follow_symlinks)

    assert cli._write_output(output, "report.md", "replacement", force=False) is False
    assert (outdir / "report.md").read_text(encoding="utf-8") == "keep this content"
    assert [entry.name for entry in outdir.iterdir()] == ["report.md"]


def test_limit_failures_name_the_users_file_not_the_private_snapshot(
    tmp_path, capsys, monkeypatch
):
    """Backends are handed a private copy, so `path.name` is a temp filename.

    The limit that trips here is the one checked inside `convert`, after the
    snapshot has been taken — exactly where the user's own filename would
    otherwise be lost.
    """
    source = tmp_path / "quarterly-report.txt"
    source.write_text(BODY, encoding="utf-8")
    monkeypatch.setattr(cli, "DEFAULT_LIMITS", ConversionLimits(max_output_chars=10))

    assert cli.main([str(source), "-o", str(tmp_path / "out")]) == 1

    reported = capsys.readouterr().out
    assert "output limit exceeded: quarterly-report.txt" in reported
    assert "d2md-input-" not in reported


def test_dangling_output_symlink_is_not_followed_without_force(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text(BODY, encoding="utf-8")
    outdir = tmp_path / "out"
    outdir.mkdir()
    protected = tmp_path / "would-be-created.txt"
    destination = outdir / "report.md"
    make_symlink_or_skip(destination, protected)

    assert cli.main([str(source), "-o", str(outdir)]) == 1
    assert destination.is_symlink()
    assert not protected.exists()


def test_output_directory_symlink_is_rejected(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text(BODY, encoding="utf-8")
    actual_outdir = tmp_path / "actual-out"
    actual_outdir.mkdir()
    linked_outdir = tmp_path / "linked-out"
    make_symlink_or_skip(
        linked_outdir, actual_outdir, target_is_directory=True
    )

    assert cli.main([str(source), "-o", str(linked_outdir)]) == 2
    assert not (actual_outdir / "report.md").exists()


def test_rejected_output_directory_link_is_named_as_a_link(tmp_path, capsys):
    """O_NOFOLLOW answers a symlinked component with ELOOP — or, on macOS
    alongside O_DIRECTORY, with ENOTDIR. Echoing that errno made `-o /tmp/out`
    report `Not a directory: 'tmp'`, which sends the user looking for a problem
    with a directory that is perfectly fine. The portable path already says
    what actually happened; both should.
    """
    source = tmp_path / "report.txt"
    source.write_text(BODY, encoding="utf-8")
    actual_outdir = tmp_path / "actual-out"
    actual_outdir.mkdir()
    linked_outdir = tmp_path / "linked-out"
    make_symlink_or_skip(linked_outdir, actual_outdir, target_is_directory=True)

    assert cli.main([str(source), "-o", str(linked_outdir / "nested")]) == 2

    message = capsys.readouterr().err
    assert "symbolic link or reparse point" in message
    assert "linked-out" in message
    assert not (actual_outdir / "nested").exists()


def test_trusted_override_disables_only_resource_limits(tmp_path, monkeypatch):
    source = tmp_path / "large.txt"
    source.write_text(BODY, encoding="utf-8")
    monkeypatch.setattr(cli, "DEFAULT_LIMITS", ConversionLimits(max_input_bytes=16))

    assert cli.main([str(source), "-o", str(tmp_path / "limited")]) == 1
    assert (
        cli.main(
            [str(source), "-o", str(tmp_path / "trusted"), "--unsafe-unlimited"]
        )
        == 0
    )


def test_plain_conversion_enforces_a_configurable_input_limit(tmp_path):
    source = tmp_path / "large.txt"
    source.write_text(BODY, encoding="utf-8")

    with pytest.raises(ConversionError, match="input limit"):
        convert(source, limits=ConversionLimits(max_input_bytes=16))


def test_public_convert_uses_a_snapshot_when_the_source_changes_after_preflight(
    tmp_path, monkeypatch
):
    source = tmp_path / "report.txt"
    safe = "This is the approved document content and it is long enough."
    replacement = "This is attacker replacement content and it is long enough."
    source.write_text(safe, encoding="utf-8")
    original_read_text = convert_module.read_text

    def replace_source_then_read(path, max_bytes=None, max_chars=None):
        source.write_text(replacement, encoding="utf-8")
        return original_read_text(
            path, max_bytes=max_bytes, max_chars=max_chars
        )

    monkeypatch.setattr(convert_module, "read_text", replace_source_then_read)

    assert convert(source).markdown == safe


def test_public_convert_rejects_a_symlinked_parent_component(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "report.txt"
    source.write_text(BODY, encoding="utf-8")
    linked = tmp_path / "linked"
    make_symlink_or_skip(linked, actual, target_is_directory=True)

    with pytest.raises(ConversionError, match="symbolic link"):
        convert(linked / "report.txt")


def test_public_convert_reports_the_original_name_after_snapshot_failure(
    tmp_path, monkeypatch
):
    source = tmp_path / "customer-report.txt"
    source.write_text(BODY, encoding="utf-8")

    def reject(snapshot, _limits):
        raise ConversionError(f"fixture rejected: {snapshot.name}")

    monkeypatch.setattr(convert_module, "_validate_input", reject)

    with pytest.raises(ConversionError) as error:
        convert(source)

    assert "customer-report.txt" in str(error.value)
    assert "d2md-input-" not in str(error.value)


def test_cli_reports_the_original_name_after_its_snapshot_failure(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "customer-report.txt"
    source.write_text(BODY, encoding="utf-8")

    def reject(snapshot, **_kwargs):
        raise ConversionError(f"fixture rejected: {snapshot.name}")

    monkeypatch.setattr(cli, "convert", reject)

    assert cli.main([str(source), "-o", str(tmp_path / "out")]) == 1
    output = capsys.readouterr().out
    assert "customer-report.txt" in output
    assert "d2md-input-" not in output


def test_collection_enforces_a_configurable_file_limit(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "one.txt").write_text(BODY, encoding="utf-8")
    (inbox / "two.txt").write_text(BODY, encoding="utf-8")

    with pytest.raises(ConversionError, match="file limit"):
        cli.collect([str(inbox)], max_files=1)


def test_collection_limit_also_bounds_unsupported_directory_entries(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("one.bin", "two.bin", "three.bin"):
        (inbox / name).write_bytes(b"not a supported document")

    with pytest.raises(ConversionError, match="file limit"):
        cli.collect([str(inbox)], max_files=2)


def test_cli_enforces_aggregate_output_limit_before_publication(
    tmp_path, monkeypatch, capsys
):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first source document", encoding="utf-8")
    second.write_text("second source document", encoding="utf-8")
    outdir = tmp_path / "out"
    monkeypatch.setattr(
        cli, "DEFAULT_LIMITS", ConversionLimits(max_output_chars=len(BODY))
    )
    monkeypatch.setattr(
        cli,
        "convert",
        lambda _path, **_kwargs: types.SimpleNamespace(
            markdown=BODY, backend="plain"
        ),
    )

    assert cli.main([str(first), str(second), "-o", str(outdir)]) == 1
    assert (outdir / "first.md").read_text(encoding="utf-8") == BODY
    assert not (outdir / "second.md").exists()
    assert "batch output limit exceeded" in capsys.readouterr().out


def test_terminal_display_escapes_control_characters_but_keeps_thai_text():
    rendered = cli.display_text("รายงาน\x1b]52;c;ignored\x07\nnext")

    assert rendered == "รายงาน\\x1b]52;c;ignored\\x07\\x0anext"


def test_pdf_preflight_rejects_page_limit_before_backend(tmp_path, monkeypatch):
    class Page:
        def get_size(self):
            return (100, 100)

        def close(self):
            pass

    class Document:
        def __init__(self, _path):
            self.pages = [Page(), Page()]

        def __len__(self):
            return len(self.pages)

        def __getitem__(self, index):
            return self.pages[index]

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Document))
    source = tmp_path / "many-pages.pdf"
    source.write_bytes(b"not parsed by the fake")

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(source, limits=ConversionLimits(max_pdf_pages=1))


def test_real_pdf_preflight_rejects_page_limit(tmp_path):
    pytest.importorskip("pypdfium2")
    reportlab = pytest.importorskip("reportlab")
    del reportlab
    from reportlab.pdfgen import canvas

    source = tmp_path / "many-pages.pdf"
    document = canvas.Canvas(str(source))
    for _ in range(2):
        document.drawString(72, 720, "real PDF page")
        document.showPage()
    document.save()

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(source, limits=ConversionLimits(max_pdf_pages=1))


def test_real_image_preflight_rejects_pixel_limit(tmp_path, monkeypatch):
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "large.png"
    image_module.new("RGB", (10, 10)).save(source)
    monkeypatch.setattr(convert_module, "ensure_ocr_available", lambda: None)

    with pytest.raises(ConversionError, match="image pixel limit"):
        convert(
            source,
            ocr=True,
            limits=ConversionLimits(max_page_pixels=50),
        )


def test_fast_pdf_text_limit_stops_accumulation(tmp_path, monkeypatch):
    class TextPage:
        def count_chars(self):
            return 10

        def get_text_range(self, *, index, count):
            assert (index, count) == (0, 10)
            return "0123456789"

        def close(self):
            pass

    class Page:
        def get_textpage(self):
            return TextPage()

        def close(self):
            pass

    class Document:
        def __init__(self, _path):
            self.pages = [Page()]

        def __iter__(self):
            return iter(self.pages)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Document))
    source = tmp_path / "large-text.pdf"
    source.write_bytes(b"not parsed by the fake")

    with pytest.raises(ConversionError, match="extracted text limit"):
        convert_module._via_pypdfium2(
            source, limits=ConversionLimits(max_output_chars=5)
        )


def test_fast_pdf_text_limit_is_checked_before_page_text_allocation(
    tmp_path, monkeypatch
):
    class TextPage:
        def count_chars(self):
            return 10

        def get_text_range(self, *args, **kwargs):
            raise AssertionError("page text must not be allocated above the limit")

        def close(self):
            pass

    class Page:
        def get_textpage(self):
            return TextPage()

        def close(self):
            pass

    class Document:
        def __init__(self, _path):
            self.pages = [Page()]

        def __iter__(self):
            return iter(self.pages)

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=Document)
    )
    source = tmp_path / "large-page-text.pdf"
    source.write_bytes(b"not parsed by the fake")

    with pytest.raises(ConversionError, match="extracted text limit"):
        convert_module._via_pypdfium2(
            source, limits=ConversionLimits(max_output_chars=5)
        )


def test_archive_expansion_limit_stops_before_markitdown(tmp_path):
    source = tmp_path / "large.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "x" * 32)

    with pytest.raises(ConversionError, match="archive expanded byte limit"):
        convert(source, limits=ConversionLimits(max_archive_bytes=16))


@pytest.mark.parametrize("suffix", (".zip", ".bin"))
def test_zip_payload_member_limit_is_enforced_before_markitdown_for_any_suffix(
    tmp_path, monkeypatch, suffix
):
    """A ZIP container cannot bypass archive limits by its extension."""
    source = tmp_path / f"payload{suffix}"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("first.txt", "first member")
        archive.writestr("second.txt", "second member")
    assert zipfile.is_zipfile(source)
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="archive member limit"):
        convert(source, limits=ConversionLimits(max_archive_members=1))

    assert markitdown_calls == []


@pytest.mark.parametrize("suffix", (".zip", ".bin"))
def test_generic_zip_is_rejected_before_markitdown_for_any_suffix(
    tmp_path, monkeypatch, suffix
):
    """Generic ZIP archives are not a supported document format."""
    source = tmp_path / f"archive{suffix}"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("note.txt", "small safe member")
    assert zipfile.is_zipfile(source)
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)
    limits = ConversionLimits(
        max_input_bytes=1_024,
        max_pdf_pages=1,
        max_page_pixels=1,
        max_total_pdf_pixels=1,
        max_output_chars=1_024,
        max_archive_members=1,
        max_archive_bytes=64,
    )

    with pytest.raises(
        ConversionError, match="generic ZIP archives are not supported"
    ):
        convert(source, limits=limits)

    assert markitdown_calls == []


@pytest.mark.parametrize("suffix", (".docx", ".xlsx", ".pptx", ".epub"))
def test_generic_zip_masquerading_as_document_is_rejected_before_markitdown(
    tmp_path, monkeypatch, suffix
):
    """A trusted suffix alone must not authorize recursive ZIP conversion."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("first.txt", "first member")
        archive.writestr("second.txt", "second member")

    source = tmp_path / f"payload{suffix}"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("inner.zip", inner.getvalue())
    assert zipfile.is_zipfile(source)
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match=rf"does not match \{suffix}"):
        convert(source, limits=ConversionLimits(max_archive_members=1))

    assert markitdown_calls == []


@pytest.mark.parametrize(
    ("suffix", "members"),
    (
        (
            ".docx",
            {
                "[Content_Types].xml": b"<Types />",
                "word/document.xml": b"<document />",
            },
        ),
        (
            ".xlsx",
            {
                "[Content_Types].xml": b"<Types />",
                "xl/workbook.xml": b"<workbook />",
            },
        ),
        (
            ".pptx",
            {
                "[Content_Types].xml": b"<Types />",
                "ppt/presentation.xml": b"<presentation />",
            },
        ),
        (
            ".epub",
            {
                "mimetype": b"application/epub+zip",
                "META-INF/container.xml": b"<container />",
            },
        ),
    ),
)
def test_zip_document_structure_matching_suffix_reaches_markitdown(
    tmp_path, monkeypatch, suffix, members
):
    """Normal ZIP-based document families keep their existing route."""
    source = tmp_path / f"document{suffix}"
    with zipfile.ZipFile(source, "w") as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    result = convert(source)

    assert result.backend == "markitdown"
    assert result.markdown == BODY
    assert len(markitdown_calls) == 1


def test_epub_requires_its_declared_mimetype_before_markitdown(tmp_path, monkeypatch):
    """ZIP markers alone cannot make an arbitrary archive an EPUB."""
    source = tmp_path / "payload.epub"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", "application/zip")
        archive.writestr("META-INF/container.xml", "<container />")
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda *_args, **_options: pytest.fail(
            "MarkItDown must not receive a fake EPUB"
        ),
    )

    with pytest.raises(ConversionError, match=r"does not match \.epub"):
        convert(source)


def test_markitdown_fallback_has_no_recursive_zip_converter(tmp_path, monkeypatch):
    """Defense in depth: generic ZIP expansion stays disabled after preflight."""
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    from d2md._onnx import disable_onnx_telemetry

    disable_onnx_telemetry()
    from markitdown import FileConversionException

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("first.txt", "NESTED SECRET FIRST enough text to convert")
        archive.writestr("second.txt", "NESTED SECRET SECOND enough text to convert")
    source = tmp_path / "payload.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("inner.zip", inner.getvalue())

    with pytest.raises(FileConversionException, match="DocxConverter"):
        convert_module._via_markitdown(source)


def test_markitdown_registration_excludes_generic_zip_converter():
    """A dependency update must not silently restore recursive ZIP handling."""
    converter = convert_module._build_markitdown(frozenset())
    registrations = {
        type(registration.converter).__name__ for registration in converter._converters
    }

    assert "ZipConverter" not in registrations
    assert "PdfConverter" not in registrations
    assert "ImageConverter" not in registrations


def test_non_archive_unknown_suffix_still_falls_back_to_markitdown(tmp_path, monkeypatch):
    """Format detection must not reject ordinary unknown files as ZIP archives."""
    source = tmp_path / "notes.bin"
    source.write_bytes(b"ordinary unknown input")
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    result = convert(source, limits=ConversionLimits(max_archive_members=1))

    assert result.backend == "markitdown"
    assert result.markdown == BODY
    assert len(markitdown_calls) == 1


def test_unknown_text_with_pdf_marker_still_falls_back_to_markitdown(
    tmp_path, monkeypatch
):
    """A marker in ordinary text is not enough to classify the file as PDF."""
    source = tmp_path / "notes.bin"
    source.write_bytes(b"ordinary notes mention the %PDF- marker")
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda *_args, **_options: BODY,
    )

    result = convert(source)

    assert result.backend == "markitdown"
    assert result.markdown == BODY


def test_unknown_bytes_with_image_magic_still_fall_back_to_markitdown(
    tmp_path, monkeypatch
):
    """A signature-only payload is not treated as a parseable image."""
    source = tmp_path / "notes.bin"
    source.write_bytes(b"\x89PNG\r\n\x1a\nordinary non-image bytes")
    monkeypatch.setattr(
        convert_module,
        "_run_isolated_backend",
        lambda *_args, **_options: BODY,
    )

    result = convert(source)

    assert result.backend == "markitdown"
    assert result.markdown == BODY


@pytest.mark.parametrize("suffix", (".bin", ".xls", ".msg", ".html"))
def test_renamed_pdf_page_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch, suffix
):
    """A PDF cannot bypass its page budget by using an unknown suffix."""
    pytest.importorskip("pypdfium2")
    reportlab = pytest.importorskip("reportlab")
    del reportlab
    from reportlab.pdfgen import canvas

    source = tmp_path / f"many-pages{suffix}"
    document = canvas.Canvas(str(source))
    for _ in range(2):
        document.drawString(72, 720, "real PDF page")
        document.showPage()
    document.save()
    assert source.read_bytes().startswith(b"%PDF-")
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(source, limits=ConversionLimits(max_pdf_pages=1))

    assert markitdown_calls == []


@pytest.mark.parametrize(
    "prefix", (b"ignored prefix\n", b"X" * 1_024), ids=("short", "pdfium-limit")
)
def test_prefixed_renamed_pdf_page_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch, prefix
):
    """A tolerated PDF prefix cannot hide the document from page preflight."""
    pdfium = pytest.importorskip("pypdfium2")
    reportlab = pytest.importorskip("reportlab")
    del reportlab
    from reportlab.pdfgen import canvas

    ordinary = tmp_path / "ordinary.pdf"
    document = canvas.Canvas(str(ordinary))
    for _ in range(2):
        document.drawString(72, 720, "real PDF page")
        document.showPage()
    document.save()
    source = tmp_path / "prefixed.bin"
    source.write_bytes(prefix + ordinary.read_bytes())
    header = source.read_bytes()[:2_048]
    assert not header.startswith(b"%PDF-")
    assert b"%PDF-" in header
    parsed = pdfium.PdfDocument(str(source))
    try:
        assert len(parsed) == 2
    finally:
        parsed.close()
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(source, limits=ConversionLimits(max_pdf_pages=1))

    assert markitdown_calls == []


def test_pdf_zip_polyglot_page_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch
):
    """An accepted ZIP suffix cannot hide a PDF from its page preflight."""
    pdfium = pytest.importorskip("pypdfium2")
    reportlab = pytest.importorskip("reportlab")
    del reportlab
    from reportlab.pdfgen import canvas

    source = tmp_path / "polyglot.docx"
    document = canvas.Canvas(str(source))
    for _ in range(2):
        document.drawString(72, 720, "real PDF page")
        document.showPage()
    document.save()
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("word/document.xml", "small archive member")
    assert source.read_bytes().startswith(b"%PDF-")
    assert zipfile.is_zipfile(source)
    parsed = pdfium.PdfDocument(str(source))
    try:
        assert len(parsed) == 2
    finally:
        parsed.close()
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="PDF page limit"):
        convert(source, limits=ConversionLimits(max_pdf_pages=1))

    assert markitdown_calls == []


@pytest.mark.parametrize("suffix", (".bin", ".xls", ".msg", ".html"))
def test_renamed_image_pixel_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch, suffix
):
    """An image cannot bypass its pixel budget by using an unknown suffix."""
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / f"large-image{suffix}"
    image_module.new("RGB", (10, 10)).save(source, format="PNG")
    assert source.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="image pixel limit"):
        convert(source, limits=ConversionLimits(max_page_pixels=50))

    assert markitdown_calls == []


def test_renamed_bigtiff_pixel_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch
):
    """BigTIFF uses a distinct magic header but keeps the TIFF pixel budget."""
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "large-image.bin"
    image_module.new("RGB", (10, 10)).save(source, format="TIFF", big_tiff=True)
    assert source.read_bytes().startswith(b"II+\x00")
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="image pixel limit"):
        convert(source, limits=ConversionLimits(max_page_pixels=50))

    assert markitdown_calls == []


def test_image_zip_polyglot_pixel_limit_is_enforced_before_markitdown(
    tmp_path, monkeypatch
):
    """An accepted ZIP suffix cannot hide an image from its pixel preflight."""
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "polyglot.docx"
    image_module.new("RGB", (10, 10)).save(source, format="PNG")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("word/document.xml", "small archive member")
    assert source.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert zipfile.is_zipfile(source)
    markitdown_calls = []

    def fake_markitdown(_backend, path, **_options):
        markitdown_calls.append(path)
        return BODY

    monkeypatch.setattr(convert_module, "_run_isolated_backend", fake_markitdown)

    with pytest.raises(ConversionError, match="image pixel limit"):
        convert(source, limits=ConversionLimits(max_page_pixels=50))

    assert markitdown_calls == []


def test_archive_member_limit_is_checked_before_zipfile_materializes_members(
    tmp_path, monkeypatch
):
    source = tmp_path / "many-members.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
    payload = bytearray(source.read_bytes())
    end = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<H", payload, end + 10, 10_001)
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect an over-limit member list")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="archive member limit"):
        convert(source)


def test_archive_member_limit_ignores_forged_low_end_record_count(
    tmp_path, monkeypatch
):
    source = tmp_path / "forged-member-count.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
        archive.writestr("word/styles.xml", "fixture")
    payload = bytearray(source.read_bytes())
    end = payload.rfind(b"PK\x05\x06")
    struct.pack_into("<H", payload, end + 8, 1)
    struct.pack_into("<H", payload, end + 10, 1)
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not allocate entries before validation")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="archive member limit"):
        convert(source, limits=ConversionLimits(max_archive_members=1))


def test_archive_member_limit_uses_zip64_count_over_forged_classic_count(
    tmp_path, monkeypatch
):
    source = tmp_path / "forged-zip64-member-count.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
        archive.writestr("word/styles.xml", "fixture")
    source.write_bytes(
        add_zip64_end_records(
            source.read_bytes(), zip64_entries=2, classic_entries=1
        )
    )

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not allocate entries before validation")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="archive member limit"):
        convert(source, limits=ConversionLimits(max_archive_members=1))


@pytest.mark.parametrize("prefix", [b"", b"MZ self-extracting prefix\x00"])
def test_archive_validation_accepts_consistent_zip64_end_records(tmp_path, prefix):
    source = tmp_path / "zip64.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
    source.write_bytes(
        prefix
        + add_zip64_end_records(
            source.read_bytes(), zip64_entries=1, classic_entries=0xFFFF
        )
    )

    with zipfile.ZipFile(source) as archive:
        assert archive.namelist() == ["word/document.xml"]
    convert_module._validate_zip_container(
        source, ConversionLimits(max_archive_members=1)
    )


def test_archive_validation_accepts_signature_dense_fixed_zip64_sfx(tmp_path):
    source = tmp_path / "signature-dense-fixed-zip64.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
    prefix = b"PK\x06\x06" * 256
    source.write_bytes(
        prefix
        + add_zip64_end_records(
            source.read_bytes(), zip64_entries=1, classic_entries=0xFFFF
        )
    )

    with zipfile.ZipFile(source) as archive:
        assert archive.namelist() == ["word/document.xml"]
    convert_module._validate_zip_container(
        source, ConversionLimits(max_archive_members=1)
    )


@pytest.mark.parametrize("prefix", [b"", b"MZ self-extracting prefix\x00"])
def test_archive_validation_accepts_zip64_extensible_end_records(tmp_path, prefix):
    source = tmp_path / "zip64-extensible.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    source.write_bytes(
        add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=0,
            classic_entries=0xFFFF,
            extensible_data=b"vendor extension data",
            prefix=prefix,
        )
    )

    with zipfile.ZipFile(source) as archive:
        assert archive.namelist() == []
    convert_module._validate_zip_container(
        source, ConversionLimits(max_archive_members=1)
    )


def test_zip64_discovery_caps_signature_dense_search_io():
    class CountingSource(io.BytesIO):
        def __init__(self, payload):
            super().__init__(payload)
            self.reads = 0
            self.seeks = 0

        def read(self, *args, **kwargs):
            self.reads += 1
            return super().read(*args, **kwargs)

        def seek(self, *args, **kwargs):
            self.seeks += 1
            return super().seek(*args, **kwargs)

    source = CountingSource(b"PK\x06\x06" * 4097)

    with pytest.raises(struct.error, match="too many ZIP64 end-record candidates"):
        convert_module._find_zip64_end_record(source, len(source.getvalue()), 0)

    assert source.reads == 1
    assert source.seeks == 1


@pytest.mark.parametrize("prefix", [b"", b"MZ self-extracting prefix\x00"])
def test_variable_zip64_directory_metadata_enforces_member_limit_before_zipfile(
    tmp_path, monkeypatch, prefix
):
    source = tmp_path / "variable-zip64-members.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
        archive.writestr("word/styles.xml", "fixture")
    source.write_bytes(
        prefix
        + add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=1,
            classic_entries=0xFFFF,
            classic_directory_is_zip64=True,
            extensible_data=b"vendor extension data",
        )
    )

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not allocate entries before validation")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="archive member limit"):
        convert(source, limits=ConversionLimits(max_archive_members=1))


def test_zip64_locator_without_a_record_fails_before_zipfile(tmp_path, monkeypatch):
    source = tmp_path / "missing-zip64-record.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    source.write_bytes(add_zip64_locator_without_record(source.read_bytes()))

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect a locator without its record")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert_module._validate_zip_container(source, ConversionLimits())


def test_zip64_locator_with_a_truncated_record_fails_before_zipfile(
    tmp_path, monkeypatch
):
    source = tmp_path / "truncated-zip64-record.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    payload = bytearray(
        add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=0,
            classic_entries=0,
            extensible_data=b"vendor extension data",
        )
    )
    locator = payload.rfind(b"PK\x06\x07")
    del payload[locator - 1]
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect a truncated ZIP64 record")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert_module._validate_zip_container(source, ConversionLimits())


def test_zip64_locator_rejects_ambiguous_declared_records(tmp_path, monkeypatch):
    source = tmp_path / "ambiguous-zip64-record.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    payload = bytearray(
        b"MZ self-extracting prefix\x00"
        + add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=0,
            classic_entries=0xFFFF,
            extensible_data=b"vendor extension data",
        )
    )
    locator = payload.rfind(b"PK\x06\x07")
    payload[:4] = b"PK\x06\x06"
    struct.pack_into("<Q", payload, 4, locator - 12)
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect ambiguous ZIP64 metadata")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert_module._validate_zip_container(source, ConversionLimits())


def test_zip64_locator_rejects_variable_record_with_fixed_suffix_decoy(
    tmp_path, monkeypatch
):
    source = tmp_path / "fixed-suffix-zip64-decoy.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    fixed_decoy = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 0, 0, 0, 0
    )
    source.write_bytes(
        add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=0,
            classic_entries=0xFFFF,
            extensible_data=fixed_decoy,
        )
    )

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect ambiguous ZIP64 metadata")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert_module._validate_zip_container(source, ConversionLimits())


@pytest.mark.parametrize("record_size", [44, 2**64 - 1])
def test_archive_validation_rejects_unbounded_zip64_extensible_record(
    tmp_path, monkeypatch, record_size
):
    source = tmp_path / "invalid-zip64-extensible.docx"
    with zipfile.ZipFile(source, "w"):
        pass
    payload = bytearray(
        add_zip64_end_records(
            source.read_bytes(),
            zip64_entries=0,
            classic_entries=0,
            extensible_data=b"vendor extension data",
        )
    )
    struct.pack_into("<Q", payload, 4, record_size)
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect malformed ZIP64 metadata")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert_module._validate_zip_container(source, ConversionLimits())


def test_archive_validation_rejects_central_directory_record_past_its_bounds(
    tmp_path, monkeypatch
):
    source = tmp_path / "invalid-central-directory.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "fixture")
    payload = bytearray(source.read_bytes())
    central_directory = payload.find(b"PK\x01\x02")
    struct.pack_into("<H", payload, central_directory + 28, 0xFFFF)
    source.write_bytes(payload)

    def must_not_open_zip(*_args, **_kwargs):
        raise AssertionError("ZipFile must not inspect invalid central metadata")

    monkeypatch.setattr(convert_module.zipfile, "ZipFile", must_not_open_zip)

    with pytest.raises(ConversionError, match="cannot inspect document archive safely"):
        convert(source)
