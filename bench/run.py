"""Run engines over the corpus and print CER per script, with timings.

    python bench/run.py CORPUS_DIR ENGINE [ENGINE ...]

Every engine is measured twice, because the gap between the two is the whole
usability story:

    told    the engine is handed the correct language for the document
    auto    the engine gets no hint, exactly as a user dropping a folder on it

`told` is the engine's ceiling. `auto` is what someone actually gets. An engine
can be excellent at `told` and useless at `auto` — Apple Vision reads Thai
perfectly when asked for Thai and returns eleven characters when asked for
English, and nothing in its output says which happened.

Engines:

    pypdfium2          the --fast path, no OCR at all — the floor
    docling-auto       Docling's default, whatever it picks
    docling-ocrmac     Docling + Apple Vision
    docling-rapidocr   Docling + RapidOCR
    docling-tesseract  Docling + Tesseract (needs TESSDATA_PREFIX)
    ocrmac-probe       Apple Vision, language chosen by confidence probe

Results are written to results-ENGINE.json beside the corpus, so a slow run
survives the terminal.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from detect import choose, choose_scored  # noqa: E402
from legacy_safe import display_text  # noqa: E402
from score import cer, cer_bag, grade  # noqa: E402

SCRIPTS = ["en", "th", "ja", "zh", "ko", "hi", "vi", "de", "ru", "ar", "zt"]
ENGINE_NAMES = {
    "docling-auto",
    "docling-ocrmac",
    "docling-rapidocr",
    "docling-tesseract",
    "easyocr",
    "ensemble",
    "ocrmac-probe",
    "pypdfium2",
    "rapidocr",
    "rapidocr-server",
    "tesseract-direct",
}

# The same script under six naming schemes. Getting these wrong is the single
# most common reason an engine is reported as "not supporting Thai".
LANG_CODE = {
    "ocrmac": {
        "en": "en-US", "th": "th-TH", "ja": "ja-JP",
        "zh": "zh-Hans", "ko": "ko-KR", "hi": None,  # Vision has no Devanagari
        "vi": "vi-VT", "de": "de-DE", "ru": "ru-RU",
        "ar": "ar-SA", "zt": "zh-Hant",
    },
    "rapidocr": {
        "en": "en", "th": "th", "ja": "japan",
        "zh": "ch", "ko": "korean", "hi": "devanagari",
        "vi": "latin", "de": "latin", "ru": "cyrillic",
        "ar": "arabic", "zt": "chinese_cht",
    },
    "tesseract": {
        "en": "eng", "th": "tha", "ja": "jpn",
        "zh": "chi_sim", "ko": "kor", "hi": "hin",
    },
    "easyocr": {
        "en": "en", "th": "th", "ja": "ja",
        "zh": "ch_sim", "ko": "ko", "hi": "hi",
    },
}

#: What an engine gets in `auto` mode: every language it supports, in a fixed
#: order. For engines that take the list as an ordered preference rather than a
#: set — Vision does — this is exactly the trap being measured.
def auto_langs(engine: str) -> list[str]:
    return [c for c in LANG_CODE[engine].values() if c]


def _converter(engine: str | None, langs: list[str]):
    from docling.datamodel.accelerator_options import (
        AcceleratorDevice,
        AcceleratorOptions,
    )
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.accelerator_options = AcceleratorOptions(
        num_threads=os.cpu_count() or 4, device=AcceleratorDevice.AUTO
    )
    opts.do_ocr = True

    if engine:
        from docling.models.factories import get_ocr_factory

        ocr = get_ocr_factory(allow_external_plugins=False).create_options(
            kind=engine, lang=langs
        )
        # These pages are scans; there is no text layer to prefer.
        ocr.force_full_page_ocr = True
        opts.ocr_options = ocr

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def _page_images(path: Path, scale: float = 2.0):
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(path))
    try:
        for page in doc:
            yield page.render(scale=scale).to_pil()
            page.close()
    finally:
        doc.close()


def vision_reader():
    """Per-page Apple Vision: {script: (text, confidence)} for one image."""
    from ocrmac import ocrmac

    supported = {s: c for s, c in LANG_CODE["ocrmac"].items() if c}

    def read(img, scripts=None):
        out = {}
        for script in scripts or supported:
            if script not in supported:
                continue
            res = ocrmac.OCR(
                img,
                language_preference=[supported[script]],
                recognition_level="accurate",
            ).recognize()
            out[script] = (
                (" ".join(r[0] for r in res), sum(r[1] for r in res) / len(res))
                if res
                else ("", 0.0)
            )
        return out

    return read


def rapid_reader(tier: str = "mobile"):
    """Per-page RapidOCR: {script: (text, confidence)} for one image."""
    import numpy as np
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    V4, V5 = OCRVersion.PPOCRV4, OCRVersion.PPOCRV5
    MOBILE, SERVER = ModelType.MOBILE, ModelType.SERVER
    spec = {
        "en": (LangRec.EN, V5, MOBILE),
        "th": (LangRec.TH, V5, MOBILE),
        "ja": (LangRec.JAPAN, V4, MOBILE),
        "zh": (LangRec.CH, V4, SERVER if tier == "server" else MOBILE),
        "ko": (LangRec.KOREAN, V4, MOBILE),
        "hi": (LangRec.DEVANAGARI, V5, MOBILE),
        # Latin and Cyrillic are script models, not language models: one set of
        # weights for every language written in them. Traditional Chinese has
        # its own, and exists only in v4.
        "vi": (LangRec.LATIN, V5, MOBILE),
        "de": (LangRec.LATIN, V5, MOBILE),
        "ru": (LangRec.CYRILLIC, V5, MOBILE),
        "ar": (LangRec.ARABIC, V5, MOBILE),
        "zt": (LangRec.CHINESE_CHT, V4, MOBILE),
    }
    engines: dict[str, object] = {}

    def read(img, scripts=None):
        arr = np.array(img.convert("RGB"))
        out = {}
        for script in scripts or spec:
            if script not in engines:
                lang, ver, mt = spec[script]
                engines[script] = RapidOCR(params={
                    "Rec.lang_type": lang, "Rec.ocr_version": ver,
                    "Rec.model_type": mt, "Global.log_level": "error",
                })
            res = engines[script](arr)
            out[script] = (
                (" ".join(res.txts), float(np.mean(res.scores)))
                if res and res.txts
                else ("", 0.0)
            )
        return out

    return read


def easyocr_reader():
    """Per-page EasyOCR: {script: (text, confidence)} for one image.

    EasyOCR groups its languages into compatible sets and refuses combinations
    that cross them, so one reader is built per script rather than one for all.
    English rides along with each — every set allows it, and real documents mix
    it in.
    """
    import easyocr
    import numpy as np

    code = {
        "en": ["en"], "th": ["th", "en"], "ja": ["ja", "en"],
        "zh": ["ch_sim", "en"], "ko": ["ko", "en"], "hi": ["hi", "en"],
        "vi": ["vi", "en"], "de": ["de", "en"], "ru": ["ru", "en"],
        "ar": ["ar", "en"], "zt": ["ch_tra", "en"],
    }
    readers: dict[str, object] = {}

    def read(img, scripts=None):
        arr = np.array(img.convert("RGB"))
        out = {}
        for script in scripts or code:
            if script not in readers:
                readers[script] = easyocr.Reader(code[script], gpu=False, verbose=False)
            res = readers[script].readtext(arr)
            out[script] = (
                (" ".join(r[1] for r in res), sum(r[2] for r in res) / len(res))
                if res
                else ("", 0.0)
            )
        return out

    return read


def make_runner(name: str):
    """Return run(path, script|None) -> text. `script` is None in auto mode."""

    if name == "easyocr":
        read = easyocr_reader()

        def run(path, script=None):
            out = []
            for img in _page_images(path):
                got = read(img, [script] if script else None)
                out.append(got[script][0] if script else choose(got)[1])
            return "\n".join(out)

        return run

    if name == "ensemble":
        # Apple Vision and RapidOCR fail on different scripts — Vision has no
        # Devanagari and RapidOCR cannot read Korean — so run both and keep
        # whichever answer scores higher. The scores are comparable because
        # both are confidence weighted by script fit, not raw engine numbers.
        vision, rapid = vision_reader(), rapid_reader()

        def run(path, script=None):
            scripts = [script] if script else None
            out = []
            for img in _page_images(path):
                picks = []
                for read in (vision, rapid):
                    got = read(img, scripts)
                    if got:
                        picks.append(choose_scored(got))
                out.append(max(picks, key=lambda p: p[2])[1] if picks else "")
            return "\n".join(out)

        return run

    if name == "pypdfium2":
        import pypdfium2

        def run(path, script=None):
            doc = pypdfium2.PdfDocument(str(path))
            try:
                out = []
                for page in doc:
                    tp = page.get_textpage()
                    out.append(tp.get_text_range())
                    tp.close()
                    page.close()
                return "\n".join(out)
            finally:
                doc.close()

        return run

    if name.startswith("rapidocr"):
        # Shares rapid_reader's model table rather than keeping its own. The
        # two used to be separate copies, and adding five languages to one of
        # them produced KeyError on every new script — the duplication was the
        # bug, not the omission.
        read = rapid_reader("server" if name.endswith("-server") else "mobile")

        def run(path, script=None):
            out = []
            for img in _page_images(path):
                got = read(img, [script] if script else None)
                out.append(got[script][0] if script else choose(got)[1])
            return "\n".join(out)

        return run

    if name == "tesseract-direct":
        # Docling's tesseract CLI wrapper raises KeyError('text') on this
        # machine, so the engine is driven directly. That also measures the
        # engine rather than the integration.
        import subprocess
        import tempfile

        code = {"en": "eng", "th": "tha", "ja": "jpn",
                "zh": "chi_sim", "ko": "kor", "hi": "hin"}

        def one(script, img):
            with tempfile.NamedTemporaryFile(suffix=".png") as f:
                img.save(f.name)
                r = subprocess.run(
                    ["tesseract", f.name, "stdout", "-l", code[script]],
                    capture_output=True, text=True,
                )
            # Tesseract reports no usable per-page confidence on stdout, so
            # script fit carries the whole auto decision here.
            return r.stdout, 1.0

        def run(path, script=None):
            out = []
            for img in _page_images(path):
                if script:
                    out.append(one(script, img)[0])
                else:
                    out.append(choose({s: one(s, img) for s in code})[1])
            return "\n".join(out)

        return run

    if name == "ocrmac-probe":
        # Ask Vision once per candidate language and keep the answer it is most
        # confident in. Confidence separates cleanly — 1.00 for the right
        # language against 0.30 for the wrong one — so this needs no threshold,
        # only a maximum. Costs one pass per candidate.
        from ocrmac import ocrmac

        supported = {s: c for s, c in LANG_CODE["ocrmac"].items() if c}

        def one(script, img):
            res = ocrmac.OCR(
                img,
                language_preference=[supported[script]],
                recognition_level="accurate",
            ).recognize()
            if not res:
                return "", 0.0
            return " ".join(r[0] for r in res), sum(r[1] for r in res) / len(res)

        def run(path, script=None):
            if script and script not in supported:
                raise RuntimeError("Apple Vision has no Devanagari model")
            out = []
            for img in _page_images(path):
                if script:
                    out.append(one(script, img)[0])
                else:
                    out.append(choose({s: one(s, img) for s in supported})[1])
            return "\n".join(out)

        return run

    engine = name.replace("docling-", "")
    if engine == "auto":
        conv = _converter(None, [])
        return lambda path, script=None: conv.convert(
            str(path)
        ).document.export_to_markdown()

    cache: dict[tuple, object] = {}

    def run(path, script=None):
        code = LANG_CODE[engine].get(script) if script else None
        if script and code is None:
            raise RuntimeError(f"{engine} has no model for {script!r}")
        langs = [code] if code else auto_langs(engine)
        key = tuple(langs)
        if key not in cache:
            cache[key] = _converter(engine, langs)
        return cache[key].convert(str(path)).document.export_to_markdown()

    return run


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    corpus = Path(argv[1]).expanduser()
    truth_dir, pdf_dir = corpus / "truth", corpus / "pdf"
    if not pdf_dir.is_dir():
        print(
            f"no corpus at {display_text(corpus)} — run bench/make_corpus.py first",
            file=sys.stderr,
        )
        return 1

    requested_engines = [argument for argument in argv[2:] if not argument.startswith("--")]
    unknown = sorted(set(requested_engines) - ENGINE_NAMES)
    if unknown:
        print(
            f"unknown engine(s): {', '.join(display_text(name) for name in unknown)}",
            file=sys.stderr,
        )
        return 2

    # `told` alone is worth having as an option: an engine that needs 10-26s a
    # page told the language spends a pass per candidate working it out, which
    # at eleven scripts is minutes per page to answer a question the ceiling
    # measurement does not depend on.
    modes = (("told", True),) if "--told" in argv else (("told", True), ("auto", False))

    for name in requested_engines:
        print(f"\n=== {display_text(name)} ===", flush=True)
        try:
            t0 = time.time()
            run = make_runner(name)
            warmup = time.time() - t0
        except Exception as e:
            print(
                f"  unavailable: {display_text(type(e).__name__)}: {display_text(e)}"
            )
            continue
        print(f"  warm-up {warmup:.1f}s")
        print(f"\n  {'file':14s} {'mode':5s} {'cer_ns':>7s} {'bag':>7s} {'secs':>6s}  verdict")

        rows = []
        for pdf in sorted(
            pdf_dir.glob("*.pdf"),
            key=lambda p: (SCRIPTS.index(p.stem.split("-")[0]), p.stem),
        ):
            stem = pdf.stem
            script, variant = stem.split("-", 1)
            truth = (truth_dir / f"{stem}.txt").read_text(encoding="utf-8")

            for mode, tell in modes:
                hint = script if tell else None
                t0 = time.time()
                try:
                    pred = run(pdf, hint)
                except Exception as e:
                    pred, err = "", f"{type(e).__name__}: {e}"
                else:
                    err = None
                secs = time.time() - t0

                row = {
                    "file": stem, "script": script, "variant": variant,
                    "mode": mode,
                    "cer": cer(pred, truth),
                    "cer_ns": cer(pred, truth, ignore_space=True),
                    "cer_bag": cer_bag(pred, truth),
                    "secs": round(secs, 2), "chars": len(pred.strip()),
                    "error": err,
                    # Kept so the scoring can be changed without paying for
                    # the run again — some of these cost a minute a page.
                    "pred": pred,
                }
                rows.append(row)
                print(
                    f"  {display_text(stem):14s} {mode:5s} {row['cer_ns']:7.3f} "
                    f"{row['cer_bag']:7.3f} {secs:6.1f}"
                    f"  {(display_text(err, limit=44) if err else grade(row['cer_bag']))}",
                    flush=True,
                )

        dst = corpus / f"results-{name}.json"
        dst.write_text(
            json.dumps({"engine": name, "warmup": warmup, "rows": rows}, indent=2),
            encoding="utf-8",
        )
        print(f"\n  → {display_text(dst)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
