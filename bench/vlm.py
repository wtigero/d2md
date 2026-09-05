"""Measure a local VLM over the corpus, via Docling's MLX pipeline.

    python bench/vlm.py CORPUS_DIR SPEC_NAME [SPEC_NAME ...]

Spec names are Docling's, e.g. GLMOCR_MLX, GRANITEDOCLING_MLX, QWEN25_VL_3B_MLX.
These download weights on first use — GLM-OCR bf16 is several GB — and run on
Metal through MLX.

Kept separate from bench/run.py because the failure modes are different: a VLM
does not return a confidence, cannot be told a language, and can decline to
answer or loop. There is no `told` mode here — asking a VLM for a language is
prompt engineering, not configuration, and d2md would not do it.
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
from legacy_safe import display_text, write_text_atomic  # noqa: E402
from score import cer, cer_bag, grade  # noqa: E402

SCRIPTS = ["en", "th", "ja", "zh", "ko", "hi", "vi", "de", "ru", "ar", "zt"]


def converter(spec_name: str):
    from docling.datamodel import vlm_model_specs
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import VlmPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.vlm_pipeline import VlmPipeline

    opts = VlmPipelineOptions(vlm_options=getattr(vlm_model_specs, spec_name))
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline, pipeline_options=opts
            )
        }
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    corpus = Path(argv[1]).expanduser()
    for spec in argv[2:]:
        print(f"\n=== {display_text(spec)} ===", flush=True)
        try:
            t0 = time.time()
            conv = converter(spec)
            warmup = time.time() - t0
        except Exception as e:
            print(
                f"  unavailable: {display_text(type(e).__name__)}: "
                f"{display_text(e)}"
            )
            continue
        print(f"  built in {warmup:.1f}s (weights load on first convert)\n")
        print(f"  {'file':14s} {'cer_ns':>7s} {'bag':>7s} {'secs':>7s}  verdict")

        rows = []
        for script in SCRIPTS:
            for variant in ("clean", "noisy"):
                stem = f"{script}-{variant}"
                pdf = corpus / "pdf" / f"{stem}.pdf"
                if not pdf.exists():
                    continue
                truth = (corpus / "truth" / f"{stem}.txt").read_text(encoding="utf-8")

                t0 = time.time()
                try:
                    pred = conv.convert(str(pdf)).document.export_to_markdown()
                except Exception as e:
                    pred, err = "", f"{type(e).__name__}: {e}"
                else:
                    err = None
                secs = time.time() - t0

                row = {
                    "file": stem, "script": script, "variant": variant,
                    "mode": "vlm",
                    "cer": cer(pred, truth),
                    "cer_ns": cer(pred, truth, ignore_space=True),
                    "cer_bag": cer_bag(pred, truth),
                    "secs": round(secs, 2), "chars": len(pred.strip()),
                    "error": err,
                    # A VLM page costs tens of seconds; never pay twice to try
                    # a different metric.
                    "pred": pred,
                }
                rows.append(row)
                print(
                    f"  {stem:14s} {row['cer_ns']:7.3f} {row['cer_bag']:7.3f} {secs:7.1f}"
                    f"  {(display_text(err, limit=44) if err else grade(row['cer_bag']))}",
                    flush=True,
                )

        dst = corpus / f"results-vlm-{spec}.json"
        write_text_atomic(
            dst,
            json.dumps({"engine": spec, "warmup": warmup, "rows": rows}, indent=2),
        )
        print(f"\n  → {display_text(dst)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
