"""Bounded, deterministic Git-history analysis for repository evolution."""

from __future__ import annotations

import math
import re
import subprocess
from typing import Optional

HISTORY_BATCH = 25


def _git() -> str:
    """Resolved git binary; falls back to bare "git" so these best-effort
    helpers keep their return-empty-on-failure contract instead of raising
    GitNotFoundError (the clone that got us here already provisioned git)."""
    try:
        from api.git_exe import git_executable
        return git_executable()
    except Exception:
        return "git"
MAX_HISTORY_COMMITS = 2000
CHECKPOINT_TARGET = 20
MIN_CHECKPOINT_STRIDE = 10
MAX_CHECKPOINT_STRIDE = 100
MAX_CHECKPOINTS = 24
MAX_CHECKPOINT_LINES = 14
MAX_CHURN_LINES = 6
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def is_shallow_clone(clone_dir: Optional[str]) -> bool:
    if not clone_dir:
        return False
    try:
        out = subprocess.run(
            [_git(), "-C", clone_dir, "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_deep_clone(clone_dir: Optional[str]) -> bool:
    if not clone_dir or not is_shallow_clone(clone_dir):
        return bool(clone_dir)
    try:
        subprocess.run(
            [_git(), "-C", clone_dir, "fetch", "--unshallow", "--tags"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return not is_shallow_clone(clone_dir)


def git_history_records(clone_dir: str) -> list[dict]:
    try:
        out = subprocess.run(
            [
                _git(),
                "-C",
                clone_dir,
                "log",
                "--no-decorate",
                f"--max-count={MAX_HISTORY_COMMITS}",
                "--date=short",
                "--pretty=format:%H\x1f%h\x1f%ad\x1f%an\x1f%s",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if out.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    records = []
    for line in out.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            records.append(
                {
                    "sha": parts[0],
                    "short": parts[1],
                    "date": parts[2],
                    "author": parts[3],
                    "subject": parts[4],
                }
            )
    return records


def git_history_count(clone_dir: str) -> int:
    try:
        out = subprocess.run(
            [_git(), "-C", clone_dir, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def record_line(record: dict) -> str:
    return (
        f"{record['short']} {record['date']} {record['author']}: "
        f"{record['subject']}"
    )


def git_full_history(clone_dir: str) -> list[str]:
    return [record_line(record) for record in git_history_records(clone_dir)]


def history_stride(total: int) -> int:
    if total <= MIN_CHECKPOINT_STRIDE:
        return 0
    stride = math.ceil(total / CHECKPOINT_TARGET / 10) * 10
    stride = max(MIN_CHECKPOINT_STRIDE, min(MAX_CHECKPOINT_STRIDE, stride))
    while total // stride > MAX_CHECKPOINTS:
        stride *= 2
    return stride


def git_progress_stat(clone_dir: str, old: str, new: str) -> str:
    try:
        out = subprocess.run(
            [
                _git(),
                "-C",
                clone_dir,
                "diff",
                "--shortstat",
                "--dirstat=files,0,3",
                old,
                new,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if out.returncode != 0:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join(line.strip() for line in out.stdout.splitlines() if line.strip())


_MILESTONE_RE = re.compile(
    r"^(feat|feature|release|breaking|perf|refactor)\b|!\s*:|BREAKING",
    re.IGNORECASE,
)


def pick_milestones(window: list[dict], limit: int) -> list[str]:
    if limit <= 0 or not window:
        return []
    chosen = [
        index
        for index, record in enumerate(window)
        if _MILESTONE_RE.search(record["subject"])
    ][:limit]
    if len(chosen) < limit:
        step = max(1, len(window) // (limit - len(chosen) + 1))
        for index in range(0, len(window), step):
            if index not in chosen:
                chosen.append(index)
            if len(chosen) >= limit:
                break
    return [window[index]["subject"][:110] for index in sorted(chosen)[:limit]]


def checkpoint_body(
    owner: str,
    repo: str,
    number: int,
    start: int,
    total: int,
    window: list[dict],
    churn: str,
) -> str:
    authors: dict[str, int] = {}
    for record in window:
        authors[record["author"]] = authors.get(record["author"], 0) + 1
    top = sorted(authors.items(), key=lambda item: (-item[1], item[0]))[:4]
    lines = [
        f"Progress checkpoint #{number} of {owner}/{repo} -- commits "
        f"{start + 1}-{start + len(window)} of {total}, "
        f"{window[0]['date']} to {window[-1]['date']} "
        f"({window[0]['short']}..{window[-1]['short']}).",
    ]
    if churn:
        lines.append("Churn since the previous checkpoint:")
        lines.extend(
            "  " + line for line in churn.splitlines()[:MAX_CHURN_LINES]
        )
    lines.append(
        "Authors: "
        + ", ".join(f"{name} ({count})" for name, count in top)
        + (", …" if len(authors) > len(top) else "")
        + "."
    )
    room = MAX_CHECKPOINT_LINES - len(lines) - 1
    milestones = pick_milestones(window, room)
    if milestones:
        lines.append("Milestones in this window:")
        lines.extend(f"  - {subject}" for subject in milestones)
    return "\n".join(lines[:MAX_CHECKPOINT_LINES])


def git_commit_range_detailed(
    clone_dir: Optional[str], old: str, new: str
) -> list[str]:
    if not clone_dir:
        return []
    try:
        out = subprocess.run(
            [
                _git(),
                "-C",
                clone_dir,
                "log",
                "--no-decorate",
                "--date=short",
                "--pretty=format:%h %ad %an: %s",
                f"{old}..{new}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (
            [line.strip() for line in out.stdout.splitlines() if line.strip()]
            if out.returncode == 0
            else []
        )
    except (OSError, subprocess.SubprocessError):
        return []


def git_history_stats(clone_dir: str) -> str:
    try:
        commands = [
            ["rev-list", "--count", "HEAD"],
            ["shortlog", "-sne", "--all"],
            ["log", "--reverse", "--date=short", "--pretty=format:%ad", "--max-count=1"],
            ["log", "-1", "--date=short", "--pretty=format:%ad"],
        ]
        results = [
            subprocess.run(
                [_git(), "-C", clone_dir, *command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            for command in commands
        ]
    except (OSError, subprocess.SubprocessError):
        return ""
    total, authors, first, last = results
    if total.returncode != 0:
        return ""
    parts = [f"Repository history overview: {total.stdout.strip()} commits"]
    if first.returncode == 0 and last.returncode == 0 and first.stdout.strip():
        parts.append(f"spanning {first.stdout.strip()} to {last.stdout.strip()}")
    text = ", ".join(parts) + "."
    if authors.returncode == 0 and authors.stdout.strip():
        top = "\n".join(authors.stdout.strip().splitlines()[:15])
        text += f"\nContributors (commits, name, email):\n{top}"
    return text


def git_commit_range(
    clone_dir: Optional[str], old: Optional[str], new: Optional[str]
) -> list[str]:
    if not clone_dir or not old or not new or old == new:
        return []
    try:
        out = subprocess.run(
            [
                _git(),
                "-C",
                clone_dir,
                "log",
                "--oneline",
                "--no-decorate",
                f"{old}..{new}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return (
            [line for line in out.stdout.splitlines() if line.strip()]
            if out.returncode == 0
            else []
        )
    except (OSError, subprocess.SubprocessError):
        return []


def git_diff_stat(
    clone_dir: Optional[str], old: Optional[str], new: Optional[str]
) -> str:
    if not clone_dir or not old or not new or old == new:
        return ""
    try:
        out = subprocess.run(
            [_git(), "-C", clone_dir, "diff", "--stat=120", f"{old}..{new}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        if out.returncode != 0:
            return ""
        return "\n".join(out.stdout.splitlines()[-60:]).strip()
    except (OSError, subprocess.SubprocessError):
        return ""
