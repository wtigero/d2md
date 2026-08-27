"""Test one engine against one script, told the language. For filling gaps.

    python bench/gaps.py CORPUS_DIR ENGINE SCRIPT [SCRIPT ...]

`bench/run.py` sweeps everything in a fixed order and measures `auto` as well
as `told`. That is right for a comparison and wrong for a question like "can
anything read Devanagari", where Hindi sorts last and the auto pass triples the
cost of an answer that does not depend on it.

This runs only the cells asked for, only in `told` mode, and prints what the
engine actually returned — because for the scripts that are still open, the
shape of the error matters more than its size.
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from legacy_safe import display_text  # noqa: E402
from run import easyocr_reader, rapid_reader, vision_reader, _page_images  # noqa: E402
from score import cer, cer_bag, grade  # noqa: E402

READERS = {
    "vision": vision_reader,
    "rapidocr": rapid_reader,
    "easyocr": easyocr_reader,
}


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2

    corpus = Path(argv[1]).expanduser()
    engine, scripts = argv[2], argv[3:]

    t0 = time.time()
    read = READERS[engine]()
    print(f"\n=== {display_text(engine)} === built in {time.time() - t0:.1f}s\n")

    for script in scripts:
        for pdf in sorted((corpus / "pdf").glob(f"{script}-*.pdf")):
            truth = (corpus / "truth" / f"{pdf.stem}.txt").read_text(encoding="utf-8")
            t0 = time.time()
            try:
                pages = [read(img, [script])[script][0] for img in _page_images(pdf)]
                pred = "\n".join(pages)
                err = None
            except Exception as e:
                pred, err = "", f"{type(e).__name__}: {e}"
            secs = time.time() - t0

            if err:
                print(
                    f"  {display_text(pdf.stem):14s} {display_text(err)}",
                    flush=True,
                )
                continue
            bag = cer_bag(pred, truth)
            print(
                f"  {display_text(pdf.stem):14s} "
                f"cer={cer(pred, truth, True):.3f} bag={bag:.3f} "
                f"{secs:5.1f}s  {grade(bag)}",
                flush=True,
            )
            print(f"      {display_text(pred.strip(), limit=110)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
