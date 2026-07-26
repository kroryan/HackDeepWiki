#!/usr/bin/env python3
"""Fail early when a full portable build cannot fit on disk."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

DEFAULT_REQUIRED_GIB = 5.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-gib", type=float, default=DEFAULT_REQUIRED_GIB)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    usage = shutil.disk_usage(root)
    required = int(args.required_gib * 1024**3)
    print(
        f"build space: free={usage.free / 1024**3:.2f} GiB, "
        f"required={required / 1024**3:.2f} GiB"
    )
    if usage.free < required:
        print(
            "Insufficient build space. Use scripts/clean_build.py --all "
            "after preserving any artifacts you need."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
