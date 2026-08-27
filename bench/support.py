"""What can be sold: worst-case CER per language per engine.

    python bench/support.py CORPUS_DIR [--mode told] [--limit 0.10]

The comparison tables show every variant because the spread is informative.
This asks a narrower question — *would we put this language on the box* — and
for that only the worst measured case matters. A language that reads at 0.000
on a clean render and 0.73 on a photocopy is not a supported language; it is a
language that works when you are lucky.

So each cell is the maximum CER across every variant of that language, and the
verdict is against that maximum. `usable` is CER < 0.10: roughly one character
in ten wrong, which is where retrieval still works and reading starts to hurt.

The last column is the intersection — languages every engine handles — because
that is the only list that can be promised without asking which platform the
user is on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from legacy_safe import display_text, load_result, table_text, truth_file  # noqa: E402

SCRIPTS = ["en", "th", "ja", "zh", "ko", "hi", "vi", "de", "ru", "ar", "zt"]
NAMES = {
    "en": "English", "th": "Thai", "ja": "Japanese", "zh": "Chinese",
    "ko": "Korean", "hi": "Hindi", "vi": "Vietnamese", "de": "German",
    "ru": "Russian", "ar": "Arabic", "zt": "Chinese-Trad",
}

#: Scripts that share a Unicode block, and so cannot be told apart by the
#: detector. Measured to be interchangeable within each group — reading a
#: German page as Vietnamese costs nothing — so they are sold as one bucket.
BUCKETS = {
    "Latin": ["en", "de", "vi"],
    "Thai": ["th"],
    "Chinese": ["zh", "zt"],
    "Japanese": ["ja"],
    "Korean": ["ko"],
    "Cyrillic": ["ru"],
    "Arabic": ["ar"],
    "Devanagari": ["hi"],
}


def worst(rows: list[dict], script: str, mode: str) -> float | None:
    """Worst CER over every variant of `script`, or None if never attempted."""
    vals = [
        r.get("cer_bag", r["cer_ns"])
        for r in rows
        if r["script"] == script and r.get("mode", "vlm") == mode
    ]
    return max(vals) if vals else None


def main(argv: list[str]) -> int:
    corpus = Path(argv[1] if len(argv) > 1 else "corpus").expanduser()
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv else "told"
    limit = float(argv[argv.index("--limit") + 1]) if "--limit" in argv else 0.10

    from score import cer, cer_bag

    results = {}
    for path in sorted(corpus.glob("results-*.json")):
        data = load_result(path)
        for row in data["rows"]:
            if "pred" not in row:
                continue
            truth = truth_file(corpus, row["file"]).read_text(encoding="utf-8")
            row["cer"] = cer(row["pred"], truth)
            row["cer_ns"] = cer(row["pred"], truth, ignore_space=True)
            row["cer_bag"] = cer_bag(row["pred"], truth)
        results[data["engine"]] = data["rows"]

    engines = [e for e, rows in results.items() if any(
        r.get("mode", "vlm") == mode for r in rows)]
    if not engines:
        print(
            f"no rows in mode {display_text(mode)!r} under {display_text(corpus)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nWorst CER across all variants — mode: {display_text(mode)}, "
        f"usable < {limit}\n"
    )
    print(
        f"| {'Language':12s} | "
        + " | ".join(f"{table_text(e):16s}" for e in engines)
        + " | all three |"
    )
    print("|" + "|".join(["-" * 14] + ["-" * 18] * len(engines) + ["-" * 11]) + "|")

    supported: dict[str, set] = {e: set() for e in engines}
    for script in SCRIPTS:
        cells, ok_all = [], True
        for e in engines:
            w = worst(results[e], script, mode)
            if w is None:
                cells.append("not measured")
                ok_all = False
            elif w > limit:
                cells.append(f"{w:.3f}  ✗")
                ok_all = False
            else:
                cells.append(f"{w:.3f}")
                supported[e].add(script)
        print(f"| {NAMES[script]:12s} | " + " | ".join(f"{c:16s}" for c in cells)
              + f" | {'YES' if ok_all else '—':9s} |")

    print("\nPer engine, languages under the limit:")
    for e in engines:
        got = [NAMES[s] for s in SCRIPTS if s in supported[e]]
        print(f"  {table_text(e):16s} {len(got):2d}  {', '.join(got)}")

    common = set.intersection(*supported.values()) if supported else set()
    print(f"\n  {'every engine':16s} {len(common):2d}  "
          f"{', '.join(NAMES[s] for s in SCRIPTS if s in common)}")

    # The all-engines intersection is only as good as the weakest engine, and
    # one weak engine collapses it. Pairs say which combination is worth
    # shipping — a language supported by a macOS engine and one portable engine
    # can be promised everywhere, whatever the third does.
    import itertools

    print("\nPairs — the honest cross-platform claim is the best of these:")
    best = (0, ())
    for a, b in itertools.combinations(engines, 2):
        both = supported[a] & supported[b]
        if len(both) > best[0]:
            best = (len(both), (a, b))
        print(f"  {table_text(a)} + {table_text(b)}: {len(both):2d}  "
              f"{', '.join(NAMES[s] for s in SCRIPTS if s in both)}")
    if best[1]:
        print(
            f"\n  best pair: {table_text(best[1][0])} + "
            f"{table_text(best[1][1])} covering {best[0]}"
        )

    print("\nAs script buckets — languages inside a bucket come free, because the")
    print("detector cannot tell them apart and does not need to:")
    for name, scripts in BUCKETS.items():
        have = [s for s in scripts if s in common]
        if have:
            print(f"  {name:12s} verified via {', '.join(NAMES[s] for s in have)}")

    _platforms(results, supported, mode)
    return 0


#: Which engines can run where. Apple Vision is macOS-only and needs nothing
#: installed; the other two run anywhere at the cost of a download.
PLATFORMS = {
    "macOS": ["ocrmac-probe", "rapidocr-mobile", "easyocr"],
    "Linux / Windows": ["rapidocr-mobile", "easyocr"],
}


def _median_secs(engine_rows: list[dict], mode: str) -> float:
    vals = sorted(
        r["secs"] for r in engine_rows
        if r.get("mode", "vlm") == mode and not r.get("error")
    )
    return vals[len(vals) // 2] if vals else float("inf")


def _platforms(results, supported, mode) -> None:
    """Per platform, the cheapest engine per language that clears the bar.

    Cheapest rather than best, deliberately. RapidOCR reads English and Chinese
    at 0.000 and does it fifteen times faster than EasyOCR, so it wins those
    two outright; everywhere it falls short something slower takes over. The
    result is what a user on each platform actually gets, which is the only
    form of "supported languages" worth printing.
    """
    speed = {e: _median_secs(rows, mode) for e, rows in results.items()}

    for platform, available in PLATFORMS.items():
        here = [e for e in available if e in supported]
        plan, unsupported = {}, []
        for script in SCRIPTS:
            options = [e for e in here if script in supported[e]]
            if not options:
                unsupported.append(script)
                continue
            plan.setdefault(min(options, key=lambda e: speed[e]), []).append(script)

        total = sum(len(v) for v in plan.values())
        print(f"\n{platform} — {total} of {len(SCRIPTS)} languages")
        for engine, scripts in plan.items():
            worst_here = max(worst(results[engine], s, mode) or 0.0 for s in scripts)
            print(f"  {table_text(engine):16s} {speed[engine]:5.1f}s/page  worst {worst_here:.3f}"
                  f"  {', '.join(NAMES[s] for s in scripts)}")
        if unsupported:
            print(f"  {'unsupported':16s} {'':18s}"
                  f"  {', '.join(NAMES[s] for s in unsupported)}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
