# Changelog

All notable changes are recorded here.

## [Unreleased]

No changes yet.

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
