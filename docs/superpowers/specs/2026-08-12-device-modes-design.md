# CPU and Accelerator Device Modes

Status: Implemented and manually verified on macOS, Ubuntu, and Windows

## Context

`d2md` currently lets Docling select its accelerator automatically. That is a
good default for normal conversions, but it prevents reproducible CPU-versus-GPU
benchmarks and gives users no way to avoid an accelerator with compatibility or
memory constraints.

Docling exposes five device choices: `auto`, `cpu`, `cuda`, `mps`, and `xpu`.
The public interface should mirror those choices instead of introducing a
generic `gpu` value whose meaning changes between NVIDIA, Apple, and Intel
hardware.

## Goals

- Add `--device {auto,cpu,cuda,mps,xpu}` to the CLI.
- Add the same optional selection to the Python conversion API.
- Keep `auto` as the default and preserve existing behavior for callers that do
  not select a device.
- Make an explicit accelerator selection strict: never silently fall back to
  CPU when the requested accelerator is unavailable.
- Keep converter instances isolated by device so CPU and accelerator runs can
  be compared in one process without reusing the wrong model instance.
- Preserve eager CUDA inference on Pascal GPUs while avoiding their unsupported
  Triton compilation path.

## Non-goals

- Device selection will not change routing between Docling, MarkItDown,
  pypdfium2, and direct text reading.
- It will not make MarkItDown, pypdfium2, direct text reading, or RapidOCR use a
  GPU when those paths do not already do so.
- It will not add automatic performance benchmarking or publish benchmark
  claims. The benchmark matrix is a separate follow-up.
- It will not manage system drivers or install accelerator-specific PyTorch
  wheels.

## Public interface

The CLI gains one option:

```text
--device {auto,cpu,cuda,mps,xpu}
```

Examples:

```bash
d2md scans/ --device cpu
d2md scans/ --device cuda
d2md scans/ --device mps
```

The Python API gains a backward-compatible keyword argument:

```python
convert(path, fast=False, lang=None, limits=None, device="auto")
```

Unknown values are rejected. The CLI uses an argparse choice error and exits
with status 2. The Python API rejects an unknown value immediately with
`ConversionError`; an unavailable but valid accelerator is rejected only when
the conversion first reaches Docling.

## Routing semantics

Device selection applies only when a conversion reaches Docling:

| Route | Device option effect |
|---|---|
| Direct text (`.txt`, `.md`, `.csv`, `.json`, `.xml`, `.yaml`) | None |
| MarkItDown (Office, HTML, MSG, EPUB) | None |
| pypdfium2 (`--fast` on a healthy text-layer PDF) | None |
| Docling (images, normal PDF, or fast-path fallback) | Selected device is enforced |

This avoids rejecting a plain-text or Office-only job merely because an unused
accelerator is unavailable. If the same batch later reaches a Docling input,
the explicit selection is validated at that point.

`auto` delegates selection to Docling exactly as today. `cpu` always requests
CPU. `cuda`, `mps`, and `xpu` are strict and fail with a concise actionable
message when their runtime is unavailable. Explicit accelerator modes never
fall back silently to CPU because that would invalidate benchmark results and
hide deployment mistakes.

## Internal design

The implementation will use a small normalized device value shared by the CLI
and conversion layer. It will remain a string at the public boundary so
importing `d2md` does not import Docling merely to construct an enum.

`convert()` passes the normalized device through `_via_docling()` to
`_docling()`. `_docling()` maps it to Docling's `AcceleratorDevice` only when a
Docling converter is constructed. Its converter-cache key changes from
`(script, force_ocr)` to `(script, force_ocr, device)`.

Availability is checked once per converter construction:

- `cpu`: always valid.
- `cuda`: requires a CUDA-enabled PyTorch build and
  `torch.cuda.is_available()`.
- `mps`: requires `torch.backends.mps.is_available()`.
- `xpu`: requires `torch.xpu.is_available()`.
- `auto`: no preflight rejection; Docling owns automatic selection.

The current Windows compatibility behavior remains: optional Docling model
compilation is disabled because a standard Python-only Windows installation
does not include the required Visual C++ compiler path.

The Pascal compatibility rule becomes device-aware. On a CUDA device below
compute capability 7.0, compilation is disabled for `auto` and `cuda` while
CUDA eager inference stays enabled. A forced CPU run on Linux does not inspect
or inherit the Pascal limitation merely because a Pascal card is installed.

## Errors and observability

Errors name the requested mode and the unavailable runtime, for example:

```text
CUDA was requested with --device cuda, but this PyTorch installation cannot access CUDA
```

The existing per-file failure handling and non-zero batch exit status remain
unchanged. CLI progress continues to report the conversion backend. Benchmark
records will store the requested device separately rather than changing the
`Result.backend` public value.

## Test design

Implementation follows red-green TDD. Tests will prove:

1. The CLI accepts all five values, defaults to `auto`, rejects unknown values,
   and forwards the selection to `convert()`.
2. Each public value maps to the matching Docling accelerator enum.
3. Converter-cache entries are separate for CPU, CUDA, MPS, XPU, and auto.
4. Explicit CUDA, MPS, and XPU selections fail clearly when unavailable and do
   not fall back to CPU.
5. Direct, MarkItDown, and successful pypdfium2 paths do not initialize or
   validate an unused accelerator.
6. Pascal CUDA disables model compilation but retains CUDA selection; forced
   CPU on the same Linux host does not apply the Pascal rule.
7. Existing routing, security-limit, Windows, and example tests remain green.

Integration verification will run the same generated fixtures with:

- Ubuntu CPU: `--device cpu`
- Ubuntu GTX 1060: `--device cuda`
- Windows CPU: `--device cpu`
- Windows RTX 3090 Ti: `--device cuda`
- macOS CPU: `--device cpu`
- macOS Apple accelerator: `--device mps`

Hardware-specific runs require the corresponding runtime and may be recorded as
environment coverage rather than portable unit tests.

## Documentation and compatibility

README usage and platform smoke-test sections will document the new option,
strict accelerator behavior, and the fact that it only affects Docling routes.
The default remains `auto`, so existing CLI commands and Python calls keep
their current behavior.

The default remains backward compatible. The existing Docling dependency is
bounded to `docling>=2.119,<3` because 2.119 is the verified release that
exposes every advertised device enum and the typed accelerator-availability
error used to preserve strict failure semantics. No new runtime package,
distribution-name change, or release-version change is part of this feature.
