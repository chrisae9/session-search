"""Explicit capture settings and catalog-owned search defaults."""

import tomllib
from pathlib import Path


def load_providers(path: Path | None) -> dict:
    if path is None:
        return {"codex": {"capture_enabled": True}, "pi": {"capture_enabled": False}}
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if set(config) != {"providers"} or not isinstance(config["providers"], dict):
        raise ValueError("provider config requires a providers table")
    result = {}
    for name, settings in config["providers"].items():
        if name not in {"codex", "pi", "pi-legacy"} or not isinstance(settings, dict):
            raise ValueError("unsupported capture provider")
        if set(settings) - {"capture_enabled", "search_by_default"}:
            raise ValueError("unknown provider setting")
        if any(type(value) is not bool for value in settings.values()):
            raise ValueError("provider settings must be booleans")
        if name == "pi-legacy" and settings.get("capture_enabled", False):
            raise ValueError("pi-legacy is imported history; capture cannot be enabled")
        result[name] = settings
    return result
