"""Measure the engines exactly as `src/d2md` configures them.

    python bench/shipped.py CORPUS_DIR [ENGINE ...]

`bench/run.py` measures engines as the survey configured them, which is how the
survey found what to ship. It is not the same thing as measuring what shipped:
`src/d2md/ocr.py` settled on PP-OCRv6 for Chinese and Japanese and the `EN`
model for Latin, while `run.py` used per-language v4/v5 models and the `LATIN`
model. Numbers from one do not license claims about the other, and the README
was quoting the wrong set.

So this imports the package's own readers rather than reimplementing them.
Whatever `d2md` would do to a page is what gets scored.
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
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from d2md.ocr import ENGINE_SCRIPTS, READERS  # noqa: E402
from legacy_safe import display_text, write_text_atomic  # noqa: E402
from score import cer, cer_bag, grade  # noqa: E402

#: Corpus filename prefix -> the script `d2md` would route it to.
AS_SCRIPT = {
    "en": "latin", "de": "latin", "vi": "latin",
    "th": "thai", "ja": "japanese", "zh": "chinese", "zt": "chinese",
    "ko": "korean", "ru": "cyrillic", "ar": "arabic", "hi": "devanagari",
}


def main(argv: list[str]) -> int:
    corpus = Path(argv[1] if len(argv) > 1 else "corpus").expanduser()
    engines = argv[2:] or list(READERS)
    unknown = sorted(set(engines) - set(READERS))
    if unknown:
        print(
            f"unknown engine(s): {', '.join(display_text(name) for name in unknown)}",
            file=sys.stderr,
        )
        return 2

    import pypdfium2

    for engine in engines:
        print(f"\n=== {display_text(engine)} (as shipped) ===\n", flush=True)
        print(f"  {'file':14s} {'script':10s} {'bag':>7s} {'secs':>6s}  verdict",
              flush=True)
        rows = []
        for pdf in sorted((corpus / "pdf").glob("*.pdf")):
            prefix = pdf.stem.split("-")[0]
            script = AS_SCRIPT.get(prefix)
            if not script or not ENGINE_SCRIPTS[engine].get(script):
                continue  # this engine does not claim this script
            truth = (corpus / "truth" / f"{pdf.stem}.txt").read_text(encoding="utf-8")

            t0 = time.time()
            try:
                doc = pypdfium2.PdfDocument(str(pdf))
                pages = [p.render(scale=2.0).to_pil() for p in doc]
                doc.close()
                pred = "\n".join(READERS[engine](img, script).text for img in pages)
                err = None
            except Exception as e:
                pred, err = "", f"{type(e).__name__}: {e}"
            secs = time.time() - t0

            row = {
                "file": pdf.stem, "script": prefix, "variant": pdf.stem.split("-", 1)[1],
                "mode": "told", "routed_as": script,
                "cer": cer(pred, truth),
                "cer_ns": cer(pred, truth, ignore_space=True),
                "cer_bag": cer_bag(pred, truth),
                "secs": round(secs, 2), "chars": len(pred.strip()),
                "error": err, "pred": pred,
            }
            rows.append(row)
            print(
                f"  {display_text(pdf.stem):14s} {script:10s} "
                f"{row['cer_bag']:7.3f} {secs:6.1f}"
                f"  {(display_text(err, limit=40) if err else grade(row['cer_bag']))}",
                flush=True,
            )

        dst = corpus / f"results-shipped-{engine}.json"
        write_text_atomic(
            dst,
            json.dumps({"engine": f"shipped-{engine}", "rows": rows}, indent=2),
        )
        print(f"\n  → {display_text(dst)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
