# Production Benchmark Matrix

Status: Implemented; formal six-run hardware evidence is pending

The [lean installation and explicit OCR design](2026-08-13-lean-installation-and-explicit-ocr-design.md)
now passes macOS, Ubuntu, and Windows functional acceptance. This matrix
measures the current explicit production commands: the lightweight default,
`--ocr`, `--docling`, and `--docling --ocr`. The deprecated fast flag is
not a benchmark dimension.

## Context

`d2md` now has explicit CPU and accelerator modes and has passed functional
smoke tests on macOS, Ubuntu, and Windows. Those smoke timings are useful
execution evidence, but they are not controlled performance measurements.

The repository already contains two useful synthetic data sets:

- `examples/generate.py` produces all 25 supported file-extension fixtures and
  a manifest with expected marker text.
- `bench/make_corpus.py` produces controlled OCR pages from which the benchmark
  selects ten language representatives and multiple scan variants, with ground
  truth suitable for the existing CER scorers.

The existing `bench/run.py` is an OCR-engine survey. It constructs engines
directly and therefore cannot license claims about the production
`d2md.convert()` routing, device policy, fallback behavior, or caching.

## Goals

- Measure the production conversion path by operating system, device,
  document type, language, and actual backend method.
- Separate model/converter initialization from steady-state conversion time.
- Record enough machine, dependency, corpus, and commit metadata to reproduce
  or reject every result.
- Measure process memory and PyTorch device-allocator memory without adding a
  runtime dependency to the package.
- Survive terminal, SSH, and individual-scenario failures without losing a
  long run.
- Generate validated JSON, CSV, and Markdown evidence before changing public
  performance claims.

## Non-goals

- This is not a comparison of every OCR library or Docling pipeline option.
- It will not time model downloads or clear shared model caches. “Cold” means a
  fresh process/converter with required models already installed.
- It will not force GPU dimensions onto direct-read, pypdfium2-only, or
  MarkItDown-only routes that cannot use the selected device.
- It will not average languages or document types into one headline score.
- It will not claim that one machine’s speedup applies to other hardware.

## Approaches considered

### 1. Extend the existing OCR-engine survey

This would reuse the most code, but it bypasses production routing and builds
different Docling/OCR configurations. It is appropriate for engine research,
not product performance claims.

### 2. Wrap the existing smoke scripts with shell timing

This is simple and exercises production behavior, but produces only a batch
total. It cannot attribute time to a language, format, method, or initialization
event; it also has no stable schema or resumability.

### 3. Add a production-route benchmark harness (selected)

The harness calls `d2md.convert()` and reuses the existing fixtures, OCR
ground truth, and CER implementation. A controller owns scenario planning,
incremental result persistence, validation, and reporting. Isolated workers own
timing and resource sampling. This adds more code than shell timing but keeps
the evidence aligned with what users run.

## Benchmark suites

### Format and routing suite

The suite covers every entry in `examples/generated/manifest.json` and records
the actual `Result.backend`.

| Scenario | Methods | Device dimension |
|---|---|---|
| Direct text (`txt`, `md`, `csv`, `json`, `xml`, `yaml`, `yml`) | direct read | CPU baseline only |
| Office/web (`docx`, `xlsx`, `xls`, `pptx`, `html`, `htm`, `msg`, `epub`) | MarkItDown | CPU baseline only |
| Born-digital PDF | default pypdfium2 and `--docling` | default once per OS; Docling on CPU/accelerator |
| Scanned PDF | `--ocr` and `--docling --ocr` | direct OCR once per OS; Docling on CPU/accelerator |
| Images (`png`, `jpg`, `jpeg`, `tiff`, `tif`, `bmp`, `webp`) | `--ocr` and `--docling --ocr` | direct OCR once per OS; Docling on CPU/accelerator |

Non-Docling scenarios are not duplicated for each device. The format suite
uses `lang="latin"` for OCR-capable synthetic fixtures so the measurement does
not accidentally include script-detection work.

Every sample must produce the expected manifest marker and the expected route.
A routing or quality mismatch is a failed benchmark sample, not a timing.

### OCR language suite

The language suite calls production `convert()` on canonical one-page scanned
PDFs from the existing corpus with an explicit script hint. It measures the
representatives already present:

- Latin: English, German, Vietnamese
- Thai
- Japanese
- Chinese: Simplified and Traditional
- Korean
- Cyrillic: Russian
- Arabic

The benchmark count and the direct public claim are both exactly ten languages
across seven writing-system buckets.

Each platform records unsupported scripts as explicit structured outcomes.
They are never silently omitted or converted into zero-second rows. The
production route’s prediction is scored with existing `cer`, `cer_ns`, and
`cer_bag` metrics against the corpus ground truth. Public tables remain
per-language; no cross-language mean is produced.

The default performance set uses the canonical clean page for every language.
Noisy, alternate-typeface, and mixed-language variants remain quality
regressions and may be requested with `--variants all`; they are not multiplied
through every repeat in the default speed matrix.

## Platform matrix

| Platform | Python | Explicit devices |
|---|---|---|
| macOS Apple Silicon | 3.12 | `cpu`, `mps` |
| Ubuntu / GTX 1060 | 3.12 | `cpu`, `cuda` |
| Windows / RTX 3090 Ti | 3.13 | `cpu`, `cuda` |

`auto` is excluded from speed comparisons because it chooses different devices
on different machines. It remains covered by functional tests.

## Timing method

All durations use `time.perf_counter_ns()`.

The controller starts one worker for each suite/device result file. Within that
worker, scenarios are grouped by the production converter-cache key
(`script`, forced-OCR state, device, and OCR-enabled state). The first successful conversion for
each cache group is the initialization record; it is validated but excluded
from warm statistics. Direct, MarkItDown, and pypdfium2-only groups have no
model initialization record and receive one unreported warm-up operation
before sampling.

1. A fresh worker creates each required production converter on first use.
   That first operation is recorded separately as `initialization_seconds`;
   model download time is out of scope.
2. The worker then performs three warm samples. One sample may contain multiple
   operations for very fast routes so its wall time reaches at least 250 ms;
   the stored value is seconds per operation.
3. If `(max - min) / median` exceeds 10%, the worker expands to seven warm
   samples. Remaining variance is reported rather than hidden.
4. Scenario and cache-group order are fixed in the configuration and stored in
   the result so converter-cache reuse is visible and reproducible.

The report shows initialization, warm median, warm range, operation count, and
whether adaptive expansion occurred. It does not mix initialization into the
steady-state median.

## Resource measurements

The benchmark development environment explicitly declares `psutil`; the
runtime package does not depend on it. A sampler records:

- peak process RSS in MiB;
- peak PyTorch allocator usage for CUDA or XPU when that API is available;
- an explicit unavailable status for MPS, whose PyTorch backend exposes no
  comparable allocator-peak API;
- `null` plus an explanatory availability field when device memory cannot be
  observed.

The GPU value is labelled “PyTorch allocator peak,” not total board VRAM. It
must not be presented publicly as whole-device consumption.

## Result schema and provenance

Each platform/device run writes one schema-versioned JSON document containing:

- Git commit and dirty-worktree state;
- public hardware-oriented platform label, OS version and architecture, total
  RAM, selected device, and Python version;
- `d2md`, Docling, PyTorch, OCR, and benchmark-schema versions;
- configuration fingerprint, corpus/manifest hashes, scenario order, repeat
  policy, and timestamps;
- initialization records and per-scenario warm samples;
- actual backend, expected backend, success/error state, marker verification,
  output hash, and language quality metrics where applicable;
- peak RSS and observable PyTorch device allocation.

Hostnames, usernames, absolute home paths, environment variables, predictions,
and document contents are not published. Synthetic predictions may be retained
only in ignored working output for debugging.

The controller writes atomically after every completed sample. `--resume`
continues only when the schema and configuration fingerprint match; otherwise
it fails rather than mixing incompatible measurements.

Raw work files live in ignored `bench-out/`. Validated evidence promoted for a
PR lives under `bench/results/<git-sha>/` with one JSON file per platform and
device, a CSV export, and a generated Markdown summary.

## Validation gates

The promotion/report command fails unless:

1. every input uses the same clean Git commit, schema, fixture hashes, and
   benchmark configuration; promotion independently rebuilds those fixture
   hashes, device-specific scenario plans, and fingerprints from the trusted
   local checkout;
2. the required six platform/device result files are present;
3. every format scenario passes marker and route verification;
4. unsupported language rows carry the expected structured error rather than
   disappearing;
5. all required warm samples exist and contain finite, positive timings;
6. summary rows retain their OS, hardware, device, method, document type, and
   language dimensions;
7. no private paths, hostnames, raw document content, or environment values
   enter promoted artifacts.

## Components

- `bench/matrix.py`: public CLI, scenario planning, metadata, and atomic
  resume files.
- `bench/matrix_worker.py`: production conversion, calibration, warm repeats,
  resource sampling, marker checks, and language scoring.
- `bench/matrix_report.py`: cross-file validation plus deterministic CSV and
  Markdown generation.
- `tests/test_benchmark_matrix.py`: RED/GREEN coverage for planning, route and
  device deduplication, adaptive repeats, schema validation, resume mismatch,
  atomic persistence, privacy filtering, and deterministic reporting.
- `bench/README.md`: exact setup and execution commands for each machine.

The worker and report modules expose small pure functions so timing policy and
schema rules can be tested without loading Docling or requiring a GPU.

## Error handling

One failed scenario is recorded and the run continues. Controller/setup errors,
schema mismatches, a changed corpus, or a dirty public-evidence run stop the
command. Interrupted writes retain the last complete JSON document through an
atomic temporary-file replacement.

The runner records the exact public scenario key and structured error when a
conversion fails. It never fabricates a timing for a failure.

## Public claims

README performance tables are generated only after all validation gates pass.
They will state the exact hardware and software context and report per-machine
measurements such as “on the tested RTX 3090 Ti machine,” not universal
speedups. Smoke timings remain labelled as smoke evidence. The release will
link to the committed result JSON and generated summary.

## Testing strategy

Implementation follows strict RED/GREEN TDD. Unit tests use injected clocks,
fake operations, and synthetic result files; they do not mock production
Docling behavior. After unit tests pass, one tiny local integration run proves
that the harness calls the installed `d2md` package and writes a resumable
result. The complete hardware matrix is the acceptance test.
