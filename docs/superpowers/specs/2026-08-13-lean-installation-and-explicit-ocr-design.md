# Lean Installation and Explicit OCR

Status: Approved for implementation

## Context

`d2md` currently installs Docling, PyTorch, Apple Vision OCR on macOS,
RapidOCR, and every Office reader in one environment. A normal PDF conversion
also enters Docling unless the user remembers `--fast`. This makes the easiest
installation the heaviest one and lets OCR run implicitly when a PDF has no
usable text layer.

The new product rule is simpler: installation and execution must both express
whether OCR is wanted. The ordinary command reads existing text and does not
load OCR or Docling. Users install the OCR capability and pass `--ocr` when
they want pixels to be read.

An isolated macOS ARM64/Python 3.12 measurement informed this decision. The
current non-Docling document stack occupied about 213 MiB; adding Apple Vision
OCR added about 30 MiB, while adding RapidOCR added about 159 MiB. These are
design measurements, not public cross-platform size claims. Fresh Windows and
Linux measurements are required before README size claims are allowed.

## Goals

- Make the default installation exclude OCR, Docling, PyTorch, and Docling
  model downloads.
- Make OCR explicit at both installation time (`[ocr]`) and execution time
  (`--ocr`).
- Make the default PDF path read the existing text layer with pypdfium2.
- Fail clearly on scans and images when OCR was not requested instead of
  returning empty output or silently selecting a heavy backend.
- Preserve Office/web conversion and direct text reading in the base install.
- Keep Docling available as an advanced optional capability without allowing
  its mere presence to change default routing.
- Preserve all existing input limits, symlink protections, atomic writes, and
  strict explicit-device behavior.
- Implement and verify the new routes before running or publishing the formal
  benchmark matrix.

## Non-goals

- No VLM pipeline, Ollama connector, arbitrary model ID, custom endpoint, or
  external Docling plugin is added.
- The CLI will not modify its own Python environment or download missing
  capabilities automatically.
- The first implementation will not split the project into multiple
  distributions.
- No performance, installation-size, language, or memory claim will be added
  until it has been measured through the implemented production route.
- The benchmark harness is a follow-up implementation, not part of this
  refactor.

## Approaches considered

### 1. Keep one all-in-one installation

This preserves current behavior and has the lowest migration cost, but every
user receives PyTorch, Docling, models, and OCR even when they only extract
text. It also leaves the surprising implicit OCR behavior in place.

### 2. One distribution with optional capabilities (selected)

The base distribution handles existing text and common Office/web files.
`[ocr]` adds platform-appropriate OCR and `[docling]` adds the complete
advanced path. The CLI explicitly selects OCR or Docling. This uses standard
Python packaging, keeps one release/version, and gives deterministic routing.

### 3. Separate lite and OCR distributions

Names such as `d2md-lite` and `d2md-ocr` avoid extras syntax, but require
two releases, synchronized versions, upgrade rules, and conflict handling for
the same console command. That maintenance cost does not add user capability.

## Installation contract

The README leads with `uv tool`; pip and pipx remain secondary alternatives
for users who manage their own Python environments.

Base installation, without OCR or Docling:

```bash
uv tool install d2md
```

Installation with OCR:

```bash
uv tool install "d2md[ocr]"
```

Advanced installation with Docling:

```bash
uv tool install "d2md[docling]"
```

`[docling]` includes the platform OCR dependencies as well as Docling because
the complete path must be able to handle scans. There is no `[full]` alias and
no second distribution.

The dependency groups are:

- Base: direct text support, pypdfium2, MarkItDown and its advertised
  Office/web readers.
- `[ocr]` on macOS: `ocrmac`; the operating system supplies Apple Vision.
- `[ocr]` on Windows/Linux: RapidOCR and its directly imported NumPy support.
- `[docling]`: the matching OCR group plus bounded Docling/Transformers
  dependencies. PyTorch continues to be resolved by Docling; GPU-specific
  PyTorch wheels and system drivers remain the operator's responsibility.
- `[dev]`: testing, fixture generation, and every optional capability required
  for the local full suite.

All optional imports stay lazy. Importing `d2md`, reading plain text, or
processing a healthy text-layer PDF must work when neither extra is installed.

## Command contract

The primary interface has only one OCR decision:

```bash
d2md report.pdf
d2md scan.pdf --ocr
```

The first command never invokes OCR or Docling. The second permits OCR for
PDFs that lack a trustworthy text layer and for image inputs. It does not
re-OCR a healthy born-digital PDF, because doing so can corrupt identifiers
and reading order.

Docling remains an explicitly selected advanced path:

```bash
d2md report.pdf --docling
d2md scan.pdf --docling --ocr
```

`--docling` requests layout, heading, reading-order, and table reconstruction
for PDF/image inputs. Without `--ocr`, Docling OCR is disabled. With `--ocr`,
Docling may OCR pages without usable text and may force full-page OCR only for
the existing detected damaged-Thai case.

`--device {auto,cpu,cuda,mps,xpu}` is retained for `--docling` only. A
non-default device without `--docling` is a usage error; direct OCR does not
pretend to use this option. Explicit unavailable accelerators remain strict
failures and never fall back silently to CPU.

`--lang SCRIPT` requires `--ocr`; it continues to skip script detection rather
than selecting a language model independently. `--engines` remains available
and prints the OCR installation command when no OCR engine is installed.

`--fast` is retained as a deprecated compatibility no-op for one release,
because the new default PDF route is the former fast text path. It is removed
from normal usage examples and emits a concise migration warning.

The Python API preserves the existing positional argument order and adds only
keyword-only capability switches:

```python
convert(
    path,
    fast=None,
    lang=None,
    limits=None,
    device="auto",
    *,
    ocr=False,
    docling=False,
)
```

`fast=True` and an explicitly passed `fast=False` both emit a deprecation
warning and select the new default text route; only an omitted `fast=None`
stays silent. Callers must request `docling=True` instead of relying on
`fast=False` to imply Docling. Supplying `ocr=True` or `docling=True` has the
same dependency and routing requirements as the corresponding CLI flag.

## Routing and data flow

Routing depends only on arguments and input type, never on which optional
packages happen to be importable.

| Input/option | Production route |
|---|---|
| Plain text formats | Direct validated read |
| Office/web formats | MarkItDown |
| Healthy text-layer PDF, no `--docling` | pypdfium2 |
| Scanned/damaged PDF with `--ocr` | Direct page rendering plus selected OCR engine |
| Image with `--ocr` | Selected OCR engine |
| PDF/image with `--docling` | Docling, with OCR enabled only when `--ocr` is present |
| Unknown format | MarkItDown attempt, followed by normal unusable-output failure |

The direct OCR adapter renders PDFs page by page through pypdfium2 and opens
images through Pillow. It enforces the existing page, per-page pixel,
total-pixel, and output-character limits. Each page becomes one Markdown text
block separated by a blank line. It reports the actual engine in
`Result.backend` (`ocrmac` or `rapidocr`); the Docling OCR route remains
`docling+ocr`.

For a PDF without `--ocr`, trustworthy text is returned. A missing text layer
or detected damaged Thai raises an actionable error that names both required
actions: install the OCR capability and rerun with `--ocr`. An image without
`--ocr` receives the same style of error before any OCR import. There is no
automatic fallback from pypdfium2 to an installed Docling or OCR package.

## Errors and capability detection

Capability errors are distinct from conversion failures:

- `--ocr` without an installed engine fails before batch conversion and shows
  the exact `uv tool install ...[ocr]...` command.
- `--docling` without Docling fails before batch conversion and shows the
  `[docling]` installation command.
- `--lang` without `--ocr`, or an explicit non-`auto` device without
  `--docling`, exits as a CLI usage error.
- A script unsupported by the installed OCR engine remains a loud per-file
  failure; it is never silently mapped to Latin.
- Backend crashes and resource-limit failures retain their existing exception
  chaining and non-zero batch result.

Messages must not claim that an unavailable capability was attempted. They
state whether installation, the `--ocr` flag, or the requested device is
missing.

## Security and privacy

The current hostile-input boundaries remain mandatory. Direct OCR must not
bypass preflight page and pixel limits, and optional backends must not weaken
symbolic-link or output-path protections. Capability discovery uses local
imports only and does not contact a package index.

Docling's OCR factory continues with external plugins disabled. No URL,
credential, environment variable, hostname, document text, or OCR prediction
is written into public benchmark artifacts. Model downloads are allowed only
as the documented behavior of an explicitly installed Docling capability, not
as a side effect of the base or OCR-only path.

## README contract

README changes land with the implementation, not before the commands work.
The first screen shows:

1. base installation and `d2md report.pdf`;
2. an “OCR scanned documents” section with the `[ocr]` installation and
   `--ocr` command;
3. an advanced Docling section, below normal usage, for tables/layout and
   device selection.

Backend names, device choices, and model details stay outside the beginner
quick start. Existing all-in-one test counts and `~500x` timing text are
removed or explicitly archived as old-architecture evidence. New README tables
are generated or copied only from validated post-refactor results and always
name the tested OS, hardware, profile, method, and corpus.

## TDD and verification

Implementation follows strict RED/GREEN order:

1. Packaging tests prove base, OCR, Docling, and dev dependency boundaries.
2. CLI tests prove option validation, actionable missing-capability messages,
   and deprecated `--fast` behavior.
3. Routing tests prove that installed extras never change an unflagged command.
4. Direct OCR tests prove page ordering, script forwarding, backend reporting,
   empty output handling, and every existing resource limit.
5. Docling tests prove OCR is disabled without `--ocr`, enabled with it, and
   still isolated by device.
6. Security regressions and the complete supported-Python local suite pass.
7. Fresh base, `[ocr]`, and `[docling]` environments are exercised on macOS,
   Ubuntu, and Windows. Each run verifies that the imported package is the
   intended checkout before its results count.

Acceptance requires these behavioral scenarios on every applicable platform:

- base converts plain, Office/web, and born-digital PDF fixtures;
- base rejects scans/images with the actionable OCR instruction;
- `[ocr]` plus `--ocr` converts supported scanned PDF/image fixtures;
- `[docling]` converts the layout/table fixtures on CPU and the available
  accelerator;
- commands without flags behave identically in all three environments;
- `pip check`, the full test suite, fixture verification, and whitespace checks
  are clean.

## Benchmark handoff

Only after acceptance passes is the production benchmark design revised and
implemented. It will measure the three commands users actually run:

- default text extraction by OS and document type;
- `--ocr` by OS, language, document type, and actual OCR engine;
- `--docling` by OS, CPU/accelerator, document type, and OCR state.

Fresh-install footprint, initialization time, warm conversion time, process
RSS, and observable device allocation are separate metrics. Unsupported
language/platform combinations are explicit outcomes. No average across
languages and no universal GPU speedup are published.
