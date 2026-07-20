"""
Registry of imported .zim archives.

Unlike wiki_cache (LLM-generated documentation for a git repo), a .zim is a
pre-existing offline wiki dump the user points us at by absolute file path --
there is nothing to generate. We just record enough metadata to list it
alongside generated wikis on the home page and re-open it by id.

One JSON file per imported archive, under <data_root>/zim_library/{id}.json.
"""
import json
import logging
import os
import re
import time
import uuid
from typing import Optional, TypedDict

from api.data_root import get_data_root

logger = logging.getLogger(__name__)

ZIM_LIBRARY_DIR = os.path.join(get_data_root(), "zim_library")
os.makedirs(ZIM_LIBRARY_DIR, exist_ok=True)

# A folder the user can drop large .zim files into directly (via file manager
# or `cp`/`mv`) instead of typing an absolute path -- important for
# multi-gigabyte archives where copy-pasting a path is the only realistic
# option anyway, but browsing to confirm it is still friction. "Rescan" scans
# this folder and auto-registers anything not already in the library.
ZIM_DROP_DIR = os.path.join(get_data_root(), "zim_drop")
os.makedirs(ZIM_DROP_DIR, exist_ok=True)


class ZimEntry(TypedDict):
    id: str
    path: str
    title: str
    description: str
    importedAt: int
    articleCount: int


def _entry_path(entry_id: str) -> str:
    # entry_id is always our own uuid4 hex, but guard against path traversal
    # regardless -- never let a crafted id escape ZIM_LIBRARY_DIR.
    safe_id = re.sub(r"[^a-f0-9]", "", entry_id.lower())
    return os.path.join(ZIM_LIBRARY_DIR, f"{safe_id}.json")


def register(path: str, title: str, description: str, article_count: int) -> ZimEntry:
    entry: ZimEntry = {
        "id": uuid.uuid4().hex,
        "path": path,
        "title": title,
        "description": description,
        "importedAt": int(time.time() * 1000),
        "articleCount": article_count,
    }
    with open(_entry_path(entry["id"]), "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    return entry


def get(entry_id: str) -> Optional[ZimEntry]:
    try:
        with open(_entry_path(entry_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_all() -> list[ZimEntry]:
    entries: list[ZimEntry] = []
    try:
        filenames = os.listdir(ZIM_LIBRARY_DIR)
    except OSError:
        return []
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(ZIM_LIBRARY_DIR, filename), "r", encoding="utf-8") as f:
                entries.append(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Skipping unreadable zim_library entry {filename}: {e}")
    entries.sort(key=lambda e: e["importedAt"], reverse=True)
    return entries


def unregister(entry_id: str) -> bool:
    """Remove the registry entry only -- never touches the .zim file itself."""
    p = _entry_path(entry_id)
    if not os.path.isfile(p):
        return False
    os.remove(p)
    return True


def registered_paths() -> set[str]:
    return {os.path.abspath(e["path"]) for e in list_all()}


def list_drop_dir_zim_files() -> list[str]:
    """Absolute paths of every .zim file sitting directly in ZIM_DROP_DIR."""
    try:
        return [
            os.path.abspath(os.path.join(ZIM_DROP_DIR, fn))
            for fn in os.listdir(ZIM_DROP_DIR)
            if fn.lower().endswith(".zim")
        ]
    except OSError:
        return []
