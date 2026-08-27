"""Render every results-*.json in a corpus directory as one comparison table.

    python bench/report.py CORPUS_DIR [--mode auto|told|vlm]

Rows are scripts, columns are engines, cells are `bag` CER — the order-
insensitive one, because a swapped heading is not a character error. Clean and
photocopied variants are shown as `clean / noisy` rather than averaged: an
engine that holds up on a clean render and collapses on a photocopy is a
different proposition from one that is merely mediocre, and a mean hides that.

Nothing is averaged across scripts, here or anywhere else in this harness.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_safe import display_text, load_result, table_text, truth_file  # noqa: E402

SCRIPTS = ["en", "th", "ja", "zh", "ko", "hi", "vi", "de", "ru", "ar", "zt"]
NAMES = {
    "en": "English", "th": "Thai", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean", "hi": "Hindi",
    "vi": "Vietnamese", "de": "German", "ru": "Russian",
    "ar": "Arabic", "zt": "Chinese-Trad",
}


def load(corpus: Path, rescore: bool = False) -> dict[str, dict]:
    """Read every results file, optionally re-scoring the saved predictions.

    The metric has changed twice under runs that cost tens of seconds a page,
    which is why the predictions are kept. `--rescore` applies the current
    scoring to old results without running anything.
    """
    out = {}
    for path in sorted(corpus.glob("results-*.json")):
        data = load_result(path)
        if rescore:
            from score import cer, cer_bag

            for row in data["rows"]:
                if "pred" not in row:
                    continue
                truth = truth_file(corpus, row["file"]).read_text(encoding="utf-8")
                row["cer"] = cer(row["pred"], truth)
                row["cer_ns"] = cer(row["pred"], truth, ignore_space=True)
                row["cer_bag"] = cer_bag(row["pred"], truth)
        out[data["engine"]] = data
    return out


def cell(rows: list[dict], script: str, mode: str, variants: list[str]) -> str:
    got = {
        r["variant"]: r
        for r in rows
        if r["script"] == script and r.get("mode", "vlm") == mode
    }
    if not got:
        return "—"

    def fmt(variant: str) -> str:
        r = got.get(variant)
        if r is None:
            return "·"  # this script has no document of that variant
        if r.get("error"):
            return "✗"
        return f"{r.get('cer_bag', r['cer_ns']):.3f}"

    return " / ".join(fmt(v) for v in variants)


def secs(rows: list[dict], mode: str) -> str:
    vals = [
        r["secs"] for r in rows
        if r.get("mode", "vlm") == mode and not r.get("error")
    ]
    if not vals:
        return "—"
    return f"{min(vals):.1f}–{max(vals):.1f}"


def main(argv: list[str]) -> int:
    corpus = Path(argv[1] if len(argv) > 1 else "corpus").expanduser()
    mode = "auto"
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]

    results = load(corpus, rescore="--rescore" in argv)
    if not results:
        print(f"no results-*.json in {display_text(corpus)}", file=sys.stderr)
        return 1

    engines = [
        name for name, d in results.items()
        if any(r.get("mode", "vlm") == mode for r in d["rows"])
    ]
    if not engines:
        print(f"no rows in mode {display_text(mode)!r}", file=sys.stderr)
        return 1

    order = ["clean", "noisy", "alt", "mixed", "mixednoisy"]
    seen = {
        r["variant"] for d in results.values() for r in d["rows"]
        if r.get("mode", "vlm") == mode
    }
    variants = [v for v in order if v in seen] + sorted(seen - set(order))
    if "--variants" in argv:
        variants = argv[argv.index("--variants") + 1].split(",")

    print(
        f"\nCER (bag), {' / '.join(table_text(v) for v in variants)} "
        f"— mode: {display_text(mode)}\n"
    )
    head = (
        f"| {'Script':10s} | "
        + " | ".join(f"{table_text(e):34s}" for e in engines)
        + " |"
    )
    print(head)
    print("|" + "|".join("-" * (len(c) + 2) for c in head.split("|")[1:-1]) + "|")

    for script in SCRIPTS:
        cells = [cell(results[e]["rows"], script, mode, variants) for e in engines]
        print(
            f"| {NAMES[script]:10s} | "
            + " | ".join(f"{c:34s}" for c in cells)
            + " |"
        )

    print(
        f"| {'secs/page':10s} | "
        + " | ".join(f"{secs(results[e]['rows'], mode):34s}" for e in engines)
        + " |"
    )
    print("\n✗ = no model for that script, or the run failed.  · = no such document.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
