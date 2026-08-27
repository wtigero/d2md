# Findings

These measurements predate the base/OCR/Docling installation profiles. They
explain the failure shapes and routing decisions that led to the current
design; they are not benchmarks or performance claims for the current release.
The corpus was a personal machine holding 392 PDFs plus assorted Office files,
a mix of Thai and English. Individual documents are not reproduced here — they
are private and client-confidential — so what follows are aggregate numbers and
failure shapes.

## 1. Corpus shape

392 PDFs, text layer read with pypdfium2 (the backend Docling uses):

| Category | Count |
|---|---|
| Scanned — no text layer at all | 192 |
| English / non-Thai | 143 |
| Thai, text layer mangled | 30 |
| Thai, text layer clean | 27 |

Roughly half the corpus needs OCR, and 30 files *look* extractable while producing corrupt
Thai.

## 2. "Broken PDF" is usually a broken reader

The same file yields different text depending on who reads it. Counting mangled Thai files
across the same corpus:

| Reader | Files reported mangled |
|---|---|
| PDFKit (macOS) | 42 |
| pypdfium2 (Chrome's engine, used by Docling) | 30 |

pdfminer, which markitdown uses, is worse still — it does not merely drop marks, it reorders
them, because it emits glyphs in visual position order and Thai stacks vowels above and below
the consonant:

```
document     "ลิงค์ส่วนตัว"          "ประกันอุบัติเหตุส่วนบุคคล"
markitdown   "ลงิค์สว่ นตวั "         "ประกนั อุบตั ิเหตุส่วนบุคคล"
pypdfium2    "ลิงค์ส่วนตัว"          (correct)
```

A concrete case: a Thai article PDF read via PDFKit gives `ยาคูลท` and `ตนกําเนิด` — tone marks
gone, `ำ` decomposed. The same file via pypdfium2 gives `ยาคูลท์` and `ต้นกำเนิด`, correct. The
PDF was fine the whole time.

So the first move on a suspect file is to try a different reader, not to reach for OCR. The
30 files that pypdfium2 still mangles are the genuinely damaged ones.

None of these readers raise an exception on the files they mishandle.

### Detecting the genuinely damaged ones

The obvious heuristic — the ratio of tone marks (`่ ้ ๊ ๋ ์`) to Thai consonants — does **not**
work. It passed two known-broken files at 0.045 and 0.086 because the loss is partial.

Counting `ำ` (U+0E33) does work, and separates cleanly:

```
broken   thai=5003  ำ=0    ํ=0
broken   thai=6592  ำ=0    ํ=73     (decomposed into a bare nikhahit instead)
healthy  thai=3862  ำ=316  ํ=0
healthy  thai=2476  ำ=28   ํ=0
```

Thai prose of any length without a single `ำ` is effectively impossible — `คำ ทำ จำ น้ำ สำ` are
too common. Requires at least ~400 Thai consonants before the test means anything: an English
document with 66 incidental Thai characters false-positives otherwise.

The current lightweight default reads the text layer directly and has no way to
distinguish a clean extraction from a damaged one — both return a plausible
string with no error. The `ำ` test lets that route reject the second case and
ask the user to install `[ocr]` and rerun with `--ocr`. It does not invoke
Docling implicitly. Docling runs only when `--docling` is selected. The detector
lives in `encoding.thai_looks_damaged`, and it is also useful on its own to
anyone auditing a corpus destined for a pdfminer-based pipeline.

## 3. What each tool does with these files

### The route that became the default: pypdfium2 alone

Text-only extraction of a 56-page PDF, including import time:

| | Time | Tables | Thai |
|---|---|---|---|
| pypdfium2 | **0.31s** | none | correct |
| Docling | 154s (29s warm-up + ~1s/page) | reconstructed | correct |

500× faster for the same characters in this pre-profile research run. What it
cannot do is structure — no table reconstruction, no heading levels, no OCR.
That result motivated making pypdfium2 the current default for "I just need the
text into a model" while keeping explicit `--docling` for "I need the parameter
tables".

The old prototype exposed this as `d2md --fast`. That option is now a
deprecated no-op because the route became the default. The route checks its own
output and requires `[ocr]` plus `--ocr` when either problem appears:

| Condition | Test | Why |
|---|---|---|
| Scanned, whole or in part | more than 10% of pages yield under 20 characters after stripping | there is no text layer to read |
| Thai text layer damaged | §2's `ำ` test over the whole document | extraction reports success and returns corrupt text |

The 10% is a tolerance, not a measurement: it lets a long document carry the
odd genuinely blank page without requiring OCR, while any real scanned section
trips it. A false positive asks for OCR unnecessarily; a missed one puts
corrupt Thai in an index, so the threshold is deliberately cautious.

The lightweight route had not been run over the 392-PDF corpus when this
research was recorded, so what follows is a projection, not a measurement. The
192 scans have no text layer and trip the page test by definition. The 30
mangled-Thai files should trip the `ำ` test, but that test was validated against
four files, not thirty. The 27 clean-Thai and 143 non-Thai files would take the
direct route.

Running the current default over the corpus and counting actual OCR-required
results is the measurement that would settle both the 10% threshold and the 30.

What was measured in the old all-in-one architecture is a synthetic 56-page
text PDF: 0.1s convert, 0.19s wall clock including process start, and an
image-only PDF falling back and taking 22s through Docling. The current design
does not perform that fallback: the image-only PDF requires `[ocr]` plus
`--ocr`, or explicit `--docling --ocr`. The 0.31s above is the real 56-page
specification, and is the number recorded against Docling's 154s on that same
file.

### markitdown

Excellent on Office formats, unusable on PDFs.

| Input | Result |
|---|---|
| `.docx` (Thai) | correct, 0.9s |
| `.xlsx` | correct, sheet name preserved as a heading, 0.5s |
| `.pptx` (Thai) | correct, `<!-- Slide number: N -->` and image alt text preserved, 0.9s |
| PDF with a broken Thai text layer | passes the corruption straight through, plus invents an empty markdown table from the page header |
| Scanned PDF | **1 byte of output, exit code 0** |

Thai in PDFs is additionally reordered, because pdfminer emits glyphs in visual position order
and Thai stacks vowels above and below the consonant:

```
document     "ลิงค์ส่วนตัว"          "ประกันอุบัติเหตุส่วนบุคคล"
markitdown   "ลงิค์สว่ นตวั "         "ประกนั อุบตั ิเหตุส่วนบุคคล"
```

Note also that the base `markitdown` package cannot open Office files at all — the format
handlers live in extras. `markitdown[docx,xlsx,pptx]` is the useful install; `[all]` additionally
pulls three Azure SDKs, a 31 MB speech recognition stack, and a YouTube transcript client.

### Docling

The research backend was correct on the measured PDFs without forcing OCR.
The current command reaches it only with explicit `--docling`.

Verified on both directions of the problem:

- A Thai PDF with a broken text layer → `ยาคูลท์`, `โพรไบโอติกส์`, `ต้นกำเนิด` all correct.
- A 56-page born-digital English API specification → UUIDs preserved byte-for-byte, and
  parameter tables reconstructed as real markdown tables with wrapped rows merged back into
  their cells.

An earlier assumption that `force_full_page_ocr=True` was mandatory turned out to be wrong,
and actively harmful: forcing OCR on a born-digital document corrupts exactly what you were
trying to protect (`f1c2a5e8` → `f1¢2a5e8`, `API` → `AP/`) and destroys table reading order.

### PDFKit (macOS) — considered and rejected

Apple's built-in PDF framework extracts Thai correctly where pdfminer does not, and does it in
1.2s for 56 pages with nothing installed. But it yields character positions, not structure.
Reconstructing the parameter tables from coordinates failed: in the test document the gap
between table columns was 4.6pt while the gap between words was 2.9pt. No single threshold
separates them, and per-page adaptive thresholds still split words (`Prod uct`, `strin g`).

That 1.7pt margin is the whole argument for Docling's TableFormer, which works from the table
image rather than from text coordinates.

## 4. Pre-profile performance research

Measured on an Apple M3, 8 cores, 16 GB, converting a 6-page scanned PDF twice in one process
to separate model load from steady-state throughput.

This measured speed, and only speed. It did not record which language the scan was in, and
`docs/ocr.md` later established that OCR quality depends entirely on that: the same pipeline
that reads a scanned English page correctly returns almost nothing from a scanned Thai one.
Read the timings below as timings; the accuracy claims are in `ocr.md`.

| Accelerator | First call (incl. model load) | Second call | Output |
|---|---|---|---|
| CPU (pinned) | 128.0s | 6.2s | identical |
| MPS / auto | 28.8s | 5.4s | identical |

In this research run, letting Docling pick the accelerator cut warm-up by 4.4×
at no cost to the measured output. The current explicit Docling route defaults
to `AcceleratorDevice.AUTO` and caches converters per device and OCR state.

Steady state is roughly 1s/page. Office files are 0.0–1.1s per file.

## 5. Encoding detection gets Thai wrong

`charset_normalizer` on a 27-byte CP874 Thai file:

```
input     ทดสอบภาษาไทย cp874 น้ำจำกัด
detected  cp949 (Korean)
returned  럽姦봉虜壘력 cp874 백斷丹磯
```

Confidently wrong, no exception. `d2md` therefore attempts CP874 before consulting a
detector, and accepts the result only if it actually contains Thai characters — which is what
keeps genuinely Korean or Cyrillic files from being forced through the Thai path.
