"""Application settings: typed defaults, validation, persistence, side effects.

Settings are stored as JSON values in the ``app_setting`` table so the UI's
sticky state survives browser changes, reinstalls, and the move from browser
mode to the desktop shell. Unknown keys are rejected so typos never become
silent dead state.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from filmex.models import AppSetting
from filmex.services import exiftool_locator as locator
from filmex.services import naming

RECENT_FOLDERS_MAX = 20
THEMES = ("dark", "paper")
IMPORT_MODES = ("copy", "move")


class SettingsError(ValueError):
    """Invalid key or value in a settings update."""


@dataclass(frozen=True)
class Spec:
    default: Any
    validate: Callable[[Any], Any]


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError("expected a string or null")
    value = value.strip()
    return value or None


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError("expected a string")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SettingsError("expected true or false")
    return value


def _enum(allowed: tuple[str, ...]) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in allowed:
            raise SettingsError(f"expected one of {', '.join(allowed)}")
        return value

    return check


def _str_list(max_len: int) -> Callable[[Any], list[str]]:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError("expected a list of strings")
        seen: list[str] = []
        for item in value:
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
        return seen[:max_len]

    return check


def _template(value: Any) -> str:
    text = _str(value)
    try:
        return naming.validate_template(text)
    except naming.NamingError as exc:
        raise SettingsError(str(exc)) from exc


def _opt_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SettingsError("expected an object or null")
    return value


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsError("expected an object")
    return value


SPECS: dict[str, Spec] = {
    # Library / workflow
    "library_root": Spec(None, _opt_str),
    "last_folder": Spec("", _str),
    "recent_folders": Spec([], _str_list(RECENT_FOLDERS_MAX)),
    "last_selection": Spec({}, _dict),
    "preview_invert": Spec(False, _bool),
    # Appearance
    "theme": Spec("dark", _enum(THEMES)),
    "window_geometry": Spec(None, _opt_dict),
    # Tools
    "exiftool_path": Spec(None, _opt_str),
    # Naming system (phase 3) and import (phase 4) — stored now so the
    # settings UI can expose them without another migration.
    "naming_template": Spec(naming.DEFAULT_TEMPLATE, _template),
    "naming_place_suffix": Spec(True, _bool),
    "import_mode": Spec("copy", _enum(IMPORT_MODES)),
}


def defaults() -> dict[str, Any]:
    return {key: json.loads(json.dumps(spec.default)) for key, spec in SPECS.items()}


def get_all(session: Session) -> dict[str, Any]:
    """All settings, stored values merged over defaults."""
    result = defaults()
    for row in session.exec(select(AppSetting)).all():
        if row.key not in SPECS:
            continue
        try:
            result[row.key] = json.loads(row.value_json)
        except json.JSONDecodeError:
            continue
    return result


def get(session: Session, key: str) -> Any:
    if key not in SPECS:
        raise SettingsError(f"unknown setting: {key}")
    return get_all(session)[key]


def update(session: Session, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a partial update, then return all settings."""
    if not isinstance(patch, dict):
        raise SettingsError("expected an object of settings")
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        spec = SPECS.get(key)
        if spec is None:
            raise SettingsError(f"unknown setting: {key}")
        try:
            cleaned[key] = spec.validate(value)
        except SettingsError as exc:
            raise SettingsError(f"{key}: {exc}") from exc

    now = datetime.now(UTC).isoformat()
    for key, value in cleaned.items():
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value_json=json.dumps(value), updated_at=now)
        else:
            row.value_json = json.dumps(value)
            row.updated_at = now
        session.add(row)
    session.commit()

    _apply_side_effects(cleaned)
    return get_all(session)


def remember_folder(session: Session, folder: str) -> dict[str, Any]:
    """Push a folder to the front of the recent list and set it as last used."""
    current = get_all(session)
    recent = [folder] + [f for f in current["recent_folders"] if f != folder]
    return update(session, {"last_folder": folder, "recent_folders": recent})


def apply_runtime_settings(session: Session) -> None:
    """Push persisted settings into process-level state at startup."""
    _apply_side_effects(get_all(session))


def _apply_side_effects(values: dict[str, Any]) -> None:
    if "exiftool_path" in values:
        locator.configure(values["exiftool_path"])
