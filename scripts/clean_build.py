"""Remove only explicitly known build outputs, with workspace-root guards."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

TARGETS = {
    "frontend": (".next",),
    "pyinstaller": ("dist", "api/dist", "api/build"),
    "appdir": ("AppDir",),
    "assets": ("bin", "tiktoken_cache"),
    "tools": (
        "appimagetool-x86_64.AppImage",
        "appimage-runtime-x86_64",
    ),
    "image": ("HackDeepWiki-x86_64.AppImage",),
}


def _remove(root: Path, relative: str) -> None:
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise RuntimeError(f"Unsafe cleanup target: {target}")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    for group in TARGETS:
        parser.add_argument(f"--{group}", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    selected = set(TARGETS) if args.all else {
        group for group in TARGETS if getattr(args, group)
    }
    if not selected:
        parser.error("select at least one target group or --all")
    root = Path(__file__).resolve().parents[1]
    targets = [item for group in sorted(selected) for item in TARGETS[group]]
    for relative in targets:
        target = root / relative
        print(f"{'would remove' if args.dry_run else 'removing'} {target}")
        if not args.dry_run:
            _remove(root, relative)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
