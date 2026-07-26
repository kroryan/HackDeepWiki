"""Repository filesystem inspection routes."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.config import DEFAULT_EXCLUDED_DIRS, DEFAULT_EXCLUDED_FILES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["repositories"])


def _is_excluded_file(name: str) -> bool:
    if name in DEFAULT_EXCLUDED_FILES:
        return True
    return any(
        pattern.startswith("*.") and name.endswith(pattern[1:])
        for pattern in DEFAULT_EXCLUDED_FILES
    )


@router.get("/local_repo/structure")
async def get_local_repo_structure(
    path: str | None = Query(None, description="Path to local repository"),
):
    if not path:
        return JSONResponse(
            status_code=400,
            content={"error": "No path provided. Please provide a 'path' query parameter."},
        )
    if not os.path.isdir(path):
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {path}"},
        )

    try:
        file_tree_lines: list[str] = []
        readme_content = ""
        excluded_dirs = {
            directory.strip("./").rstrip("/") for directory in DEFAULT_EXCLUDED_DIRS
        } | {"__pycache__", "node_modules", ".venv"}
        for root, dirs, files in os.walk(path):
            dirs[:] = [
                directory
                for directory in dirs
                if not directory.startswith(".") and directory not in excluded_dirs
            ]
            for filename in files:
                if (
                    filename.startswith(".")
                    or filename == "__init__.py"
                    or filename == ".DS_Store"
                    or _is_excluded_file(filename)
                ):
                    continue
                relative_dir = os.path.relpath(root, path)
                relative_file = (
                    os.path.join(relative_dir, filename)
                    if relative_dir != "."
                    else filename
                )
                file_tree_lines.append(relative_file)
                if filename.lower() == "readme.md" and not readme_content:
                    try:
                        with open(
                            os.path.join(root, filename), encoding="utf-8"
                        ) as readme:
                            readme_content = readme.read()
                    except OSError as exc:
                        logger.warning("Could not read README.md: %s", exc)
        return {"file_tree": "\n".join(sorted(file_tree_lines)), "readme": readme_content}
    except OSError:
        logger.exception("Error processing local repository")
        return JSONResponse(
            status_code=500,
            content={"error": "Error processing local repository"},
        )
