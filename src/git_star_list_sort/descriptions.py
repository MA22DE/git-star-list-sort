"""Load committed List descriptions and turn them into Jev criteria."""

from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

LISTS_KEY = "lists"


def load_descriptions(path: Path) -> dict[str, str]:
    """Return the List descriptions committed for the model, or an empty mapping."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    lists = document.get(LISTS_KEY)
    if not isinstance(lists, dict):
        return {}
    return {
        name: description
        for name, description in lists.items()
        if isinstance(name, str) and isinstance(description, str) and description
    }


def build_criteria(
    lists: list[dict[str, Any]], descriptions: dict[str, str]
) -> dict[str, str]:
    """Map each List id to ``"<name>: <description>"`` for Jev's choice question.

    A committed description wins over the live GitHub List description; when
    neither exists the value is just ``"<name>: "``.
    """
    criteria: dict[str, str] = {}
    for item in lists:
        name = item["name"]
        description = (
            descriptions.get(name) or item.get("description") or ""  # type: ignore[assignment]
        )
        criteria[item["id"]] = f"{name}: {description}"
    return criteria
