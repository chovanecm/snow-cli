"""Storage for OAuth tokens: the OS keyring, or a private file if it is unavailable."""
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional

import keyring


class Tokens(NamedTuple):
    access_token: str
    refresh_token: Optional[str]
    expires_at: float


class TokenStore:
    """Holds one instance and client's tokens as a single JSON value."""

    def __init__(self, instance: str, client_id: str, fallback_file: Path):
        self._service = f"snow-cli:{instance}"
        self._account = f"oauth-token:{client_id}"
        self._fallback_file = fallback_file

    def load(self) -> Optional[Tokens]:
        return self._parse(self._read_keyring()) or self._parse(self._read_file())

    def save(self, tokens: Tokens) -> None:
        text = json.dumps(tokens._asdict())
        try:
            keyring.set_password(self._service, self._account, text)
        except Exception:
            print(
                f"Warning: system keyring not available, OAuth tokens stored in {self._fallback_file}",
                file=sys.stderr,
            )
            self._write_file(text)
            return
        self._remove_file()

    def clear(self) -> None:
        try:
            keyring.delete_password(self._service, self._account)
        except Exception:
            pass
        self._remove_file()

    @staticmethod
    def _parse(text: Optional[str]) -> Optional[Tokens]:
        if not text:
            return None
        try:
            data = json.loads(text)
            return Tokens(data["access_token"], data.get("refresh_token"), float(data["expires_at"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def _read_keyring(self) -> Optional[str]:
        try:
            return keyring.get_password(self._service, self._account)
        except Exception:
            return None

    def _read_file(self) -> Optional[str]:
        try:
            return self._fallback_file.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_file(self, text: str) -> None:
        directory = self._fallback_file.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_path = tempfile.mkstemp(dir=str(directory))  # created with mode 600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(temp_path, str(self._fallback_file))
        except BaseException:
            os.unlink(temp_path)
            raise

    def _remove_file(self) -> None:
        try:
            self._fallback_file.unlink()
        except OSError:
            pass
