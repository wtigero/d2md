#!/usr/bin/env python3
"""Run one installation profile against the synthetic example corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import subprocess
import sys

try:
    from .verify import verify
except ImportError:  # direct script execution
    from verify import verify


REPOSITORY = Path(__file__).resolve().parents[1]
PROFILE_CHOICES = ("base", "ocr", "docling")
DEVICE_CHOICES = ("auto", "cpu", "cuda", "mps", "xpu")


def build_command(
    profile: str,
    files: list[Path],
    outdir: Path,
    device: str,
) -> list[str]:
    if profile not in PROFILE_CHOICES:
        raise ValueError(f"unknown profile: {profile}")
    command = [
        sys.executable,
        "-m",
        "d2md.cli",
        *map(str, files),
        "-o",
        str(outdir),
        "--force",
    ]
    if profile in {"ocr", "docling"}:
        command.append("--ocr")
    if profile == "docling":
        command.extend(("--docling", "--device", device))
    return command


def _generated_fixture(generated: Path, name: object) -> Path:
    """Return one regular manifest fixture contained by ``generated``."""
    if not isinstance(name, str) or not name:
        raise ValueError("manifest fixture path must be a non-empty string")
    relative = Path(name)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ValueError(f"manifest fixture path escapes generated directory: {name!r}")
    try:
        root = generated.resolve(strict=True)
        candidate = generated / relative
        details = candidate.lstat()
    except OSError as error:
        raise ValueError(f"missing generated fixture: {name}") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise ValueError(f"generated fixture is not a regular file: {name}")
    try:
        candidate.resolve(strict=True).relative_to(root)
    except ValueError as error:
        raise ValueError(f"manifest fixture path escapes generated directory: {name!r}") from error
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=PROFILE_CHOICES, default="base"
    )
    parser.add_argument("--device", choices=DEVICE_CHOICES, default="auto")
    parser.add_argument(
        "--generated",
        type=Path,
        default=REPOSITORY / "examples" / "generated",
    )
    parser.add_argument(
        "--converted",
        type=Path,
        default=REPOSITORY / "examples" / "converted",
    )
    args = parser.parse_args(argv)

    if args.profile != "docling" and args.device != "auto":
        parser.error("--device requires --profile docling")

    generated = args.generated.expanduser()
    converted = args.converted.expanduser()
    manifest_path = generated / "manifest.json"
    if not manifest_path.is_file():
        print(f"missing generated manifest: {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        print("generated manifest has no documents list", file=sys.stderr)
        return 2
    entries = []
    for entry in documents:
        if not isinstance(entry, dict):
            print("generated manifest has an invalid document entry", file=sys.stderr)
            return 2
        if args.profile == "base" and entry.get("capability", "base") != "base":
            continue
        entries.append(entry)
    try:
        files = [_generated_fixture(generated, entry.get("file")) for entry in entries]
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    completed = subprocess.run(
        build_command(args.profile, files, converted, args.device),
        cwd=REPOSITORY,
        check=False,
    )
    if completed.returncode:
        return completed.returncode

    passed, failed = verify(
        manifest_path,
        converted,
        profile=args.profile,
    )
    print(f"\n{passed} verified · {failed} failed ({args.profile})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
