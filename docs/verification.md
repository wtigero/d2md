# Manual verification results

This page records functional release-candidate checks completed on 2026-08-14.
Every profile was installed in an isolated environment on each operating
system. These results are manual hardware evidence; no hosted CI result is
included.

The common runtime candidate was commit `ec8cae7`. The follow-up commit that
adds this report and its documentation assertions does not change package or
runtime files.

| OS | Python | Base | OCR | Docling CPU | Accelerator | Full tests | Fixtures |
|---|---:|---|---|---|---|---:|---:|
| macOS 26.5.2 | 3.12.4 | PASS | PASS (Apple Vision) | PASS | PASS (MPS) | 162 passed, 2 skipped | Base 17/17; OCR and Docling 25/25 |
| Ubuntu 24.04.4 | 3.12.3 | PASS | PASS (RapidOCR) | PASS | PASS (CUDA, GTX 1060, cu126) | 162 passed, 2 skipped | Base 17/17; OCR and Docling 25/25 |
| Windows 11 Pro 10.0.26200 | 3.13.13 | PASS | PASS (RapidOCR) | PASS | PASS (CUDA, RTX 3090 Ti, cu130) | 162 passed, 2 skipped | Base 17/17; OCR and Docling 25/25 |

## What was checked

The full test suite ran from an isolated development environment containing
the test and Docling dependencies. Each Base, OCR, and Docling result then
came from a separate fresh environment containing only that installation
profile. The smoke scripts performed a dependency check, printed the imported
candidate package path, generated the synthetic corpus, converted the selected
fixtures, and verified their expected markers and routes.

Accelerator runs used the smoke scripts' strict GPU requirement. A missing or
unusable requested accelerator therefore failed instead of falling back to
CPU. CUDA environments also executed a tensor on the selected GPU; the macOS
environment reported Apple MPS available before the Docling MPS run.

The Windows CUDA installation was also repeated through the documented
`uv tool install --torch-backend auto` route. It selected matching
`torch 2.13.0+cu130` and `torchvision 0.28.0+cu130` wheels, exposed the RTX
3090 Ti, executed a CUDA tensor, and converted a born-digital PDF through
Docling with `--device cuda`.

## Hardware and relevant packages

- macOS: Apple M3, 16 GiB RAM; Docling 2.119.0, Torch 2.13.0,
  ocrmac 1.0.1, pypdfium2 5.13.0.
- Ubuntu: Intel Core i5-13500, GeForce GTX 1060 6 GiB, NVIDIA driver
  580.173.02; Docling 2.119.0, paired Torch/Torchvision cu126, RapidOCR
  3.9.2, pypdfium2 5.13.0.
- Windows: Intel Core i5-14500, GeForce RTX 3090 Ti 24 GiB, NVIDIA driver
  610.88; Docling 2.119.0, paired Torch/Torchvision cu130, RapidOCR 3.9.2,
  pypdfium2 5.13.0.

The default Linux CUDA wheel available during this run excluded the GTX 1060
compute capability. Installing the matching cu126 Torch and Torchvision pair
restored support. Replacing Torch alone is insufficient because a mismatched
Torchvision build prevents Docling's Transformers imports.

## Claim boundary

These are functional routing, dependency, accelerator, and synthetic-marker
checks. They are not a performance benchmark, OCR-accuracy score, language
quality comparison, or memory-usage claim. Speed and quality measurements
require a separate fixed corpus and benchmark protocol.

Model caches were not purged between profile runs, so these results do not
prove a cold or offline first run. RapidOCR may need to retrieve model files
on first use and fails explicitly when that download is unavailable.
