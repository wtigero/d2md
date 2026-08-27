"""Measure Surya over the corpus. Runs in its own interpreter, on purpose.

    ~/.surya-env/bin/python bench/surya_run.py CORPUS_DIR [SCRIPT ...]

Surya cannot share an environment with the rest of this project. Installing it
pulls transformers to 5.15, which is the version that stops Docling's layout
model compiling on MPS, and drops Pillow to 10.4, which may lose the raqm
support the corpus generator refuses to run without. Both were found by
`uv pip install --dry-run` rather than by breaking the environment first —
the same check that would have caught `mlx-vlm` doing the same thing earlier.

So this script imports nothing from the project except the scorer, which is
pure standard library, and writes results in the same shape as `bench/run.py`
so `report.py` and `support.py` can read them alongside everything else.

Surya is told nothing about the language: it has no per-language models and no
language argument, which is either its best feature or an untestable claim
depending on how it scores.
"""

from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from legacy_safe import display_text  # noqa: E402
from score import cer, cer_bag, grade  # noqa: E402

SCRIPTS = ["en", "th", "ja", "zh", "ko", "hi", "vi", "de", "ru", "ar", "zt"]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    corpus = Path(argv[1]).expanduser()
    wanted = argv[2:] or SCRIPTS

    import pypdfium2
    from PIL import Image  # noqa: F401  (surya needs it importable)
    from surya.recognition import RecognitionPredictor

    # surya-ocr 0.22 drives detection internally: one predictor, images in,
    # laid-out text out. Earlier releases wired a FoundationPredictor and a
    # DetectionPredictor together by hand.
    t0 = time.time()
    recognition = RecognitionPredictor()
    warmup = time.time() - t0
    print(f"\n=== surya === warm-up {warmup:.1f}s\n", flush=True)
    print(f"  {'file':14s} {'cer_ns':>7s} {'bag':>7s} {'secs':>6s}  verdict", flush=True)

    rows = []
    for pdf in sorted(
        (corpus / "pdf").glob("*.pdf"),
        key=lambda p: (SCRIPTS.index(p.stem.split("-")[0]), p.stem),
    ):
        script, variant = pdf.stem.split("-", 1)
        if script not in wanted:
            continue
        truth = (corpus / "truth" / f"{pdf.stem}.txt").read_text(encoding="utf-8")

        t0 = time.time()
        try:
            doc = pypdfium2.PdfDocument(str(pdf))
            images = [p.render(scale=2.0).to_pil() for p in doc]
            doc.close()
            out = recognition(images, full_page=True)
            # 0.22 returns layout blocks carrying HTML rather than plain
            # lines, so the markup comes off here. The scorer strips markdown,
            # not tags, and charging an engine for its own output format would
            # measure the wrong thing.
            pred = "\n".join(
                "\n".join(
                    re.sub(r"<[^>]+>", " ", b.html or "")
                    for b in page.blocks
                    if not b.skipped
                )
                for page in out
            )
            pred = unescape(pred)
            err = None
        except Exception as e:
            pred, err = "", f"{type(e).__name__}: {e}"
        secs = time.time() - t0

        row = {
            "file": pdf.stem, "script": script, "variant": variant, "mode": "told",
            "cer": cer(pred, truth),
            "cer_ns": cer(pred, truth, ignore_space=True),
            "cer_bag": cer_bag(pred, truth),
            "secs": round(secs, 2), "chars": len(pred.strip()),
            "error": err, "pred": pred,
        }
        rows.append(row)
        print(
            f"  {display_text(pdf.stem):14s} {row['cer_ns']:7.3f} "
            f"{row['cer_bag']:7.3f} {secs:6.1f}"
            f"  {(display_text(err, limit=44) if err else grade(row['cer_bag']))}",
            flush=True,
        )

    dst = corpus / "results-surya.json"
    dst.write_text(
        json.dumps({"engine": "surya", "warmup": warmup, "rows": rows}, indent=2),
        encoding="utf-8",
    )
    print(f"\n  → {display_text(dst)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
