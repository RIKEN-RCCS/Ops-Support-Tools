"""Environment/machine target normalization helpers."""

from __future__ import annotations

import json
import os
from typing import Any


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def environment_candidates() -> list[str]:
    return _split_csv(os.environ.get("SUPPORT_AI_ENVIRONMENT_CANDIDATES", ""))


def _raw_machine_candidates() -> list[str]:
    return _split_csv(os.environ.get("SUPPORT_AI_MACHINE_CANDIDATES", ""))


def machine_alias_map() -> dict[str, list[str]]:
    """Return canonical machine -> aliases.

    SUPPORT_AI_MACHINE_ALIAS_MAP is JSON such as:
    {"RIKYU": ["理究", "Rikyu", "rikyu", "りきゅう"]}
    """
    raw = os.environ.get("SUPPORT_AI_MACHINE_ALIAS_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, list[str]] = {}
    for canonical, aliases in parsed.items():
        canonical_text = str(canonical).strip()
        if not canonical_text:
            continue
        values: list[str] = []
        if isinstance(aliases, list):
            values = [str(alias).strip() for alias in aliases if str(alias).strip()]
        elif isinstance(aliases, str) and aliases.strip():
            values = [aliases.strip()]
        result[canonical_text] = values
    return result


def machine_candidates() -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for value in [*_raw_machine_candidates(), *machine_alias_map().keys()]:
        key = _norm(value)
        if key and key not in seen:
            seen.add(key)
            values.append(value)
    return values


def machine_alias_guide() -> str:
    parts = []
    for canonical, aliases in machine_alias_map().items():
        alias_text = ", ".join(aliases) if aliases else canonical
        parts.append(f"{canonical}=aliases({alias_text})")
    return "; ".join(parts) if parts else "(未設定)"


def canonicalize_machine(value: Any) -> tuple[str, str]:
    """Normalize a machine label.

    Returns (canonical_machine, status), where status is one of:
    exact, alias, unknown, ambiguous, empty.
    """
    text = str(value or "").strip()
    if not text:
        return "", "empty"

    lookup: dict[str, set[str]] = {}
    for canonical in _raw_machine_candidates():
        lookup.setdefault(_norm(canonical), set()).add(canonical)
    for canonical, aliases in machine_alias_map().items():
        lookup.setdefault(_norm(canonical), set()).add(canonical)
        for alias in aliases:
            lookup.setdefault(_norm(alias), set()).add(canonical)

    matches = lookup.get(_norm(text), set())
    if not matches:
        return "", "unknown"
    if len(matches) > 1:
        return "", "ambiguous"
    canonical = next(iter(matches))
    return canonical, "exact" if _norm(canonical) == _norm(text) else "alias"
