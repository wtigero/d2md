# Changelog

All notable changes are recorded here.

## [Unreleased]

## [0.2.2]

- Support Python 3.14. On macOS it needs Apple silicon and macOS 14 or later,
  where ONNX Runtime publishes 3.14 builds.

## [0.2.1]

- Add an agent skill, `skills/d2md/SKILL.md`, that tells coding agents when to
  call d2md and how to read its JSON report. The README shows how to install
  it for Claude Code.
- Accept short Office, web, and email documents, such as a one-line memo.
  Only an empty one fails. 0.2.0 refused anything under 20 characters as a
  probable scan.
- Report an input as failed when a folder or other non-file already has its
  output name. 0.2.0 reported it as skipped and the run as successful, though
  nothing was converted.

## [0.2.0]

- Refuse a PDF whose short pages are mostly image as needing OCR, even under
  the 10% blank-page tolerance. Images inside forms and scans stored as strips
  count. A scan with a stamped footer was returned as the footer alone. A
  report with a full-page photo cover or figure, and slides on a full-page
  background, now need `--ocr` too, which then OCRs every page.
- Read PDF text layers in a worker with the 30-minute deadline and 4 GiB
  ceiling, and read page sizes without loading pages. A 478 KB PDF held the
  calling process for more than 18 minutes.
- Decode HTML the way browsers do: BOM first, then the declared charset with
  browser label rules (`iso-8859-1` reads as windows-1252), then d2md's own
  Thai-aware rules. A windows-874 page came back as Chinese.
- Accept CP874 text whose Thai is a small share of the file, so an English
  invoice with one Thai word keeps it. When Thai would be a small share, Latin-1
  accents and symbols that CP874 reads as Thai, as in `Größe` or `©`, are not
  accepted.
- Check each PDF page for damaged Thai, so one healthy word cannot clear the
  rest.
- Keep folders under a directory input, skip `._name` and `~$name` files, and
  stop converting once the run's output limit is reached.
- Report a truncated or unreadable image as such, and detect the OCR script
  from up to three TIFF pages.
- Remove the `--fast` option, the `fast` and `display_name` arguments of
  `convert()`, and the unused Wikipedia, YouTube, Bing, and audio converters.
  `lang` is now the second positional argument of `convert()`.
- Report a second input that needs an output name already used in the run as
  failed, even with `--force`. Earlier versions replaced or skipped it and could
  report success.
- Write new outputs to filesystems without hard links, such as exFAT, while
  still refusing to overwrite an existing file.
- Escape PDF text that looks like a page marker, anywhere in the text, so a PDF
  cannot forge `<!-- Page number: N -->`.
- Open images as PNG, JPEG, TIFF, BMP, or WebP only, so an EPS named `.png` can
  no longer reach Ghostscript through Pillow.
- Accept short plain text files. Only an empty one fails.
- Keep d2md's own error text from backend workers instead of a generic failure.
- Copy each CLI input once, through one input-safety path shared with
  `convert()`, and name the link that stopped an input.
- Read every page of a multi-page TIFF during `--ocr`, bounded by the page and
  pixel limits. Earlier versions silently kept only the first page.
- Stop charging the default PDF text route for pixels it never renders, so long
  born-digital PDFs reach the 500-page limit.
- Read only stored XLSX cells, so a tiny sparse workbook stays cheap, and stop
  the MarkItDown worker when its memory passes 4 GiB on macOS and Linux. Windows
  has no memory ceiling yet.
- Start backend workers with the parent's import path, so `pip install --user`
  and `PYTHONPATH` installs can convert Office and web documents.
- Combine merged two-row XLSX headers and repeat section context only within
  merged ranges.
- Preserve XLSX cell line breaks as `<br>` and omit empty columns while keeping
  notes in columns without headers.
- Constrain the macOS Docling profile to Transformers versions that support
  its MPS layout model.

## [0.1.2]

- Add numbered `<!-- Page number: N -->` markers to PDF Markdown output for
  reliable page-aware RAG chunking across the default, OCR, and Docling routes.
- Annotate unsplittable multi-page Docling content with
  `<!-- Content spans pages: ... -->` and preserve it intact on its first
  provenance page rather than inventing row or character boundaries.

## [0.1.1]

- Render empty and error-valued Excel cells as blank Markdown table cells
  without deleting source text whose value is exactly `NaN`.

## [0.1.0]

- Local conversion of text files, Office/web documents, and PDFs to Markdown,
  with a lightweight default path.
- Explicit optional `ocr` and `docling` profiles for scanned documents,
  layout, reading order, headings, and tables.
- Thai-aware handling for legacy TIS-620/CP874 text and measured damaged Thai
  PDF text layers.
- Machine-friendly `--stdout`, `--json`, and `--capabilities` CLI modes for
  shell automation and AI agents.
- Resource ceilings, symbolic-link rejection, atomic output handling on POSIX,
  and explicit errors for unsupported capabilities and unsafe paths.
- Early plain-text and PDF output checks, collected-file identity binding, and
  a cumulative CLI output budget for hostile or unexpectedly large batches.
- Content-aware ZIP, PDF, and image preflight so misleading filename suffixes
  cannot skip format-specific limits; ZIP packages must match their declared
  document family, and generic recursive ZIP conversion is disabled.
- Full-string MarkItDown and Docling jobs run in reusable subprocesses, with
  output ceilings enforced before the caller accepts the result and failed
  workers discarded.
- Option-looking paths require explicit `--`/`./` disambiguation and cannot
  silently enable conversion capabilities.
- The benchmark-promotion schema filters private values from public evidence.
- The build backend is pinned for reproducible release builds.
- A hosted CI workflow covering Python 3.10–3.13 across Linux, macOS, and
  Windows, plus wheel and source-distribution smoke-test jobs.
- A coordinated security policy and a release gate that requires private
  vulnerability reporting to be enabled and verified when the repository
  becomes public.
- Manual cross-platform release-candidate verification documented in
  [docs/verification.md](docs/verification.md). Those results are historical
  manual evidence and do not replace current-revision hosted CI.
- Prominent macOS Apple Vision OCR guidance with historical, commit-pinned
  accuracy evidence, zero separate OCR-weight downloads, and an explicit tested
  hardware reference rather than an unsupported minimum claim.
