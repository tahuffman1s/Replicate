"""Passwords.

Kept in the desktop keyring through libsecret when its GObject introspection
data is around (the usual case on a GNOME or KDE desktop and inside the
AppImage), otherwise in a 0600 JSON file next to the configuration. The
file fallback is also what headless tests use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from replicate import paths

_SCHEMA_NAME = "io.github.tahuffman1s.Replicate.Secret"

try:  # pragma: no cover - depends on the desktop
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret  # type: ignore

    _schema = Secret.Schema.new(
        _SCHEMA_NAME,
        Secret.SchemaFlags.NONE,
        {"key": Secret.SchemaAttributeType.STRING},
    )
except Exception:  # noqa: BLE001
    Secret = None  # type: ignore
    _schema = None


class SecretStore:
    def __init__(self, file_path: Path | None = None, use_keyring: bool | None = None):
        self._file = file_path or paths.SECRETS_FILE
        if use_keyring is None:
            use_keyring = Secret is not None and os.environ.get("REPLICATE_NO_KEYRING") != "1"
        self._keyring = bool(use_keyring and Secret is not None)

    @property
    def backend(self) -> str:
        return "keyring" if self._keyring else "file"

    # -- file fallback -----------------------------------------------------
    def _read_file(self) -> dict[str, str]:
        if not self._file.exists():
            return {}
        with self._file.open(encoding="utf-8") as fh:
            return json.load(fh)

    def _write_file(self, data: dict[str, str]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._file)

    # -- API ----------------------------------------------------------------
    def get(self, key: str) -> str | None:
        if self._keyring:
            try:
                value = Secret.password_lookup_sync(_schema, {"key": key}, None)
                if value is not None:
                    return value
            except Exception:  # noqa: BLE001
                pass
        return self._read_file().get(key)

    def set(self, key: str, value: str) -> None:
        if self._keyring:
            try:
                Secret.password_store_sync(
                    _schema, {"key": key}, Secret.COLLECTION_DEFAULT, f"Replicate: {key}", value, None
                )
                return
            except Exception:  # noqa: BLE001
                pass
        data = self._read_file()
        data[key] = value
        self._write_file(data)

    def delete(self, key: str) -> None:
        if self._keyring:
            try:
                Secret.password_clear_sync(_schema, {"key": key}, None)
            except Exception:  # noqa: BLE001
                pass
        data = self._read_file()
        if key in data:
            del data[key]
            self._write_file(data)
