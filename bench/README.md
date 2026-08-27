# Production benchmark matrix

This is a manual, reproducible benchmark for the routes users actually run:
the lightweight default, `--ocr`, `--docling`, and `--docling --ocr`. It is
not a CI job and it does not compare unrelated OCR libraries.

Each result is tied to one clean commit, one machine label, one explicit
Docling device, fixture hashes, package versions, and the selected timing
policy. The harness records first-use initialization separately from warm
conversion time. It stores only public scenario metadata, hashes, metrics, and
resource figures: no URLs, document text, ground truth, paths, hostnames,
usernames, or environment values.

Promoted evidence for commit `7203499e58bf8e6415b3190638d0f8a689f55924`
is available in the [full 219-row result](results/7203499e58bf8e6415b3190638d0f8a689f55924/README.md).
The six raw JSON files retain the `doc2md` dependency key recorded before the
final rebrand. They are immutable historical evidence, not a compatibility alias.

## Prepare the same inputs

Run this once on the macOS preparation machine, from the repository root:

```bash
uv venv --python 3.12 .venv
uv pip install --torch-backend auto --python .venv/bin/python -e '.[dev,docling,benchmark]'
.venv/bin/python examples/generate.py
.venv/bin/python bench/make_corpus.py corpus
```

Copy the resulting `examples/generated/` and `corpus/` directories unchanged
to the Ubuntu and Windows checkouts at the same commit. They are ignored local
inputs, not files to commit. Do not regenerate them independently: matching
fixture hashes are a promotion gate. The format planner accepts a strict UTF-8
JSON manifest up to 1 MiB and 100 documents.

On the Ubuntu GTX 1060 host, select its supported CUDA build explicitly:

```bash
uv venv --python 3.12 .venv
uv pip install --torch-backend cu126 --python .venv/bin/python -e '.[dev,docling,benchmark]'
.venv/bin/python -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0), torch.version.cuda)"
```

On the Windows RTX 3090 Ti host, use Python 3.13 and its CUDA build:

```powershell
uv venv --python 3.13 .venv
uv pip install --torch-backend cu130 --python .venv\Scripts\python.exe -e '.[dev,docling,benchmark]'
.venv\Scripts\python.exe -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0), torch.version.cuda)"
.venv\Scripts\python.exe examples\generate.py
```

The exact prepared corpus is copied from the preparation machine; Windows does
not generate another one.

## Run the six hardware measurements

Start from a clean checkout at the same commit on every host. These public
labels are deliberately hardware-oriented rather than hostnames:

```bash
# macOS Apple Silicon
.venv/bin/python -m bench.matrix run --suite all --platform macos-arm64 --device cpu --output bench-out/macos-cpu.json
.venv/bin/python -m bench.matrix run --suite all --platform macos-arm64 --device mps --output bench-out/macos-mps.json

# Ubuntu GPU host
.venv/bin/python -m bench.matrix run --suite all --platform ubuntu-gtx1060 --device cpu --output bench-out/ubuntu-cpu.json
.venv/bin/python -m bench.matrix run --suite all --platform ubuntu-gtx1060 --device cuda --output bench-out/ubuntu-cuda.json
```

```powershell
# Windows GPU host
.venv\Scripts\python.exe -m bench.matrix run --suite all --platform windows-rtx3090ti --device cpu --output bench-out\windows-cpu.json
.venv\Scripts\python.exe -m bench.matrix run --suite all --platform windows-rtx3090ti --device cuda --output bench-out\windows-cuda.json
```

The CPU result includes direct/default OCR routes plus Docling CPU. An
accelerator result includes only the matching Docling route, so an unchanged
route is never reported twice as a CPU/GPU comparison.

Interrupted runs resume only when their configuration fingerprint matches:

```bash
.venv/bin/python -m bench.matrix run --suite all --platform ubuntu-gtx1060 --device cuda --output bench-out/ubuntu-cuda.json --resume
```

Raw result JSON is limited to 1 MiB for resume and promotion.

For a harness smoke check only, use one public source. This is not evidence
for a performance claim:

```bash
.venv/bin/python -m bench.matrix run --suite format --platform local-smoke --device cpu --output bench-out/local-smoke.json --only-source plain-utf8.txt
```

## Validate and promote public evidence

Before copying results, have the trusted operator on each benchmark host record
the SHA-256 of its raw JSON. Transfer each digest through a channel independent
of the raw file; a digest generated after transfer or delivered beside a
potentially altered result is not an authenticity check. Copy the six raw JSON
files to one clean checkout at the measured commit, then create a new result
directory. Promotion hashes the exact bytes it parses, verifies every trusted
digest, and rebuilds each device-specific plan, fixture hashes, and fingerprint
from that checkout's generated fixtures before writing anything. It fails if an
OS/device run is missing, a format marker/backend check failed, timing samples
are invalid, commits or fixture hashes differ, a raw JSON field is not part of
the public schema, or a private-looking value appears in the result.

Set the six `*_SHA256` variables below from those independently recorded
values before running promotion.

```bash
.venv/bin/python -m bench.matrix_report promote \
  --input bench-out/macos-cpu.json "$MACOS_CPU_SHA256" \
  --input bench-out/macos-mps.json "$MACOS_MPS_SHA256" \
  --input bench-out/ubuntu-cpu.json "$UBUNTU_CPU_SHA256" \
  --input bench-out/ubuntu-cuda.json "$UBUNTU_CUDA_SHA256" \
  --input bench-out/windows-cpu.json "$WINDOWS_CPU_SHA256" \
  --input bench-out/windows-cuda.json "$WINDOWS_CUDA_SHA256" \
  --format-manifest examples/generated/manifest.json \
  --corpus corpus \
  --output bench/results/COMMIT_SHA \
  --require macos-arm64:cpu \
  --require macos-arm64:mps \
  --require ubuntu-gtx1060:cpu \
  --require ubuntu-gtx1060:cuda \
  --require windows-rtx3090ti:cpu \
  --require windows-rtx3090ti:cuda
```

The promoted directory contains one validated JSON file per run plus
`summary.csv` and `README.md`. Free-form failure diagnostics are intentionally
excluded from the promoted JSON. Only after this command succeeds should a
README make any hardware performance claim. The summary keeps OS, device,
document type, method, language, backend, initialization, warm time, memory,
and language-quality dimensions separate; it never creates a cross-language
average.
