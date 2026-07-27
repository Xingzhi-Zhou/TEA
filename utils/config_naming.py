"""Canonical and backward-compatible SFU configuration file naming."""

from __future__ import annotations

from pathlib import Path


def canonical_config_name(function: str, number_format: str) -> str:
    """Return the release naming convention: function_format.json."""

    return f"{function.lower()}_{number_format.lower()}.json"


def resolve_config_path(
    cfg_dir: Path,
    function: str,
    number_format: str,
    legacy_name: str,
) -> Path:
    """Prefer the canonical name, falling back to the original cfg name."""

    canonical = cfg_dir / canonical_config_name(function, number_format)
    return canonical if canonical.is_file() else cfg_dir / legacy_name
