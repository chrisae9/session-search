"""Explicit capture settings and catalog-owned search defaults."""

import tomllib
from pathlib import Path


LIVE_CAPTURE_PROVIDERS = frozenset({"codex", "pi", "hermes"})


def load_providers(path: Path | None) -> dict:
    if path is None:
        return {"codex": {"capture_enabled": True}, "pi": {"capture_enabled": False},
                "hermes": {"capture_enabled": True}}
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if set(config) != {"providers"} or not isinstance(config["providers"], dict):
        raise ValueError("provider config requires a providers table")
    result = {"hermes": {"capture_enabled": True}}
    for name, settings in config["providers"].items():
        if name not in LIVE_CAPTURE_PROVIDERS | {"pi-legacy"} or not isinstance(settings, dict):
            raise ValueError("unsupported capture provider")
        if set(settings) - {"capture_enabled", "search_by_default"}:
            raise ValueError("unknown provider setting")
        if any(type(value) is not bool for value in settings.values()):
            raise ValueError("provider settings must be booleans")
        if name == "pi-legacy" and settings.get("capture_enabled", False):
            raise ValueError("pi-legacy is imported history; capture cannot be enabled")
        result.setdefault(name, {}).update(settings)
    return result
