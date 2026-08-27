# Example documents

`generate.py` creates a small, reproducible, synthetic document for every
file extension that `d2md` accepts. The generated files contain no client or
personal data and are safe to use for local smoke tests.

Generate the fixtures from the repository root:

```bash
python examples/generate.py
```

The command writes inputs and `manifest.json` to `examples/generated/`. Every
manifest entry names its expected marker and required capability:

- `base` — plain text, Office/web, and born-digital PDF fixtures;
- `ocr` — the scanned PDF and image fixtures.

## Run one profile

Use an interpreter where the matching installation profile is already
installed:

```bash
python examples/smoke.py --profile base
python examples/smoke.py --profile ocr
python examples/smoke.py --profile docling --device cpu
```

The base run selects only base fixtures. OCR and Docling runs select the full
corpus and pass the required `--ocr` or `--docling` flags explicitly. The
driver returns non-zero if conversion or marker verification fails.

## Isolated operating-system smoke tests

The Bash script works on Linux and macOS:

```bash
./scripts/smoke-linux.sh --profile base
./scripts/smoke-linux.sh --profile ocr
./scripts/smoke-linux.sh --profile docling --device cpu
./scripts/smoke-linux.sh --profile docling --device cuda --require-gpu
./scripts/smoke-linux.sh --skip-integration
```

Windows PowerShell uses the same profiles:

```powershell
.\scripts\smoke-windows.ps1 -Profile Base
.\scripts\smoke-windows.ps1 -Profile Ocr
.\scripts\smoke-windows.ps1 -Profile Docling -Device cpu
.\scripts\smoke-windows.ps1 -Profile Docling -Device cuda -RequireGpu
.\scripts\smoke-windows.ps1 -SkipIntegration
```

Each script creates two disposable environments:

- `.venv-smoke` installs `.[dev,docling]`, generates fixtures, and runs the
  complete test suite;
- `.venv-smoke-profile` installs only `.`, `.[ocr]`, or `.[docling]`, runs its
  dependency check, prints the imported `d2md` path, and executes the chosen
  example profile.

`--device`/`-Device` and the GPU requirement are valid only for the Docling
profile. `--skip-integration`/`-SkipIntegration` stops after the full test
suite, before the selected profile is installed.

Generated fixtures cover:

| Group | Extensions | Capability |
|---|---|---|
| Text | `.txt`, `.md`, `.csv`, `.json`, `.xml`, `.yaml`, `.yml` | base |
| Office and web | `.docx`, `.xlsx`, `.xls`, `.pptx`, `.html`, `.htm`, `.msg`, `.epub` | base |
| Born-digital PDF | `.pdf` | base |
| Scanned PDF and images | `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`, `.webp` | ocr |

`examples/generated/` and `examples/converted/` are disposable local output.
Regenerate them whenever the example recipe changes.
