#!/usr/bin/env python3
"""Verify converted example output against the generator manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import unicodedata

from d2md.cli import display_text


def normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def verify(
    manifest_path: Path,
    converted: Path,
    profile: str | None = None,
) -> tuple[int, int]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    passed = 0
    failed = 0
    for entry in manifest["documents"]:
        if profile == "base" and entry.get("capability", "base") != "base":
            continue
        filename = display_text(entry["file"])
        output = converted / f"{Path(entry['file']).stem}.md"
        if not output.is_file():
            print(f"FAIL {filename}: missing {display_text(output.name)}")
            failed += 1
            continue
        markdown = output.read_text(encoding="utf-8")
        if normalize(entry["expected_text"]) not in normalize(markdown):
            print(
                f"FAIL {filename}: expected text not found: "
                f"{entry['expected_text']!r}"
            )
            failed += 1
            continue
        print(f"PASS {filename}")
        passed += 1
    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--converted", type=Path, required=True)
    parser.add_argument("--profile", choices=("base", "ocr", "docling"))
    args = parser.parse_args()

    passed, failed = verify(args.manifest, args.converted, profile=args.profile)
    print(f"\n{passed} verified · {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
