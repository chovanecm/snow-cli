"""Instance storage: file persistence, keyring, and path computation."""
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

import keyring

from .token_store import TokenStore


class InstanceRepository:
    """Owns all file-system and keyring I/O for instance storage."""

    def __init__(self, snow_dir: Optional[Path] = None):
        self.snow_dir = snow_dir or Path.home() / ".snow-run"
        self.config_file = self.snow_dir / "config.json"

    # ---------- path helpers ----------

    def cookie_file_for(self, instance: str) -> Path:
        return self.snow_dir / "tmp" / instance / "cookies.txt"

    def tmp_dir_for(self, instance: str) -> Path:
        return self.snow_dir / "tmp" / instance

    def token_cache_file_for(self, instance: str) -> Path:
        return self.tmp_dir_for(instance) / "oauth_token.json"

    # ---------- CRUD ----------

    def save(self, instance: str, user: str, password: str, set_default: bool = False) -> bool:
        """Persist instance credentials. Returns True if password was stored in keyring."""
        data = self._load_json()
        if "instances" not in data:
            data["instances"] = {}
        self._delete_stored_secret(instance, data["instances"].get(instance))
        keyring_success = self._set_password_in_keyring(instance, user, password)
        data["instances"][instance] = {"user": user, "keyring": keyring_success}  # full replacement clears stale "password" key
        if not keyring_success:
            data["instances"][instance]["password"] = password
        if set_default or "default_instance" not in data:
            data["default_instance"] = instance
        self._save_json(data)
        return keyring_success

    def save_oauth(self, instance: str, client_id: str, client_secret: str, set_default: bool = False) -> bool:
        """Persist OAuth client credentials. Returns True if the secret was stored in keyring."""
        data = self._load_json()
        if "instances" not in data:
            data["instances"] = {}
        self._delete_stored_secret(instance, data["instances"].get(instance))
        keyring_success = self._set_password_in_keyring(instance, self._secret_account(client_id), client_secret)
        data["instances"][instance] = {"auth": "oauth", "client_id": client_id, "keyring": keyring_success}
        if not keyring_success:
            data["instances"][instance]["client_secret"] = client_secret
        if set_default or "default_instance" not in data:
            data["default_instance"] = instance
        self._save_json(data)
        return keyring_success

    def remove(self, instance: str):
        data = self._load_json()
        if instance not in data.get("instances", {}):
            return
        self._delete_stored_secret(instance, data["instances"][instance])
        del data["instances"][instance]
        if data.get("default_instance") == instance:
            remaining = list(data["instances"])
            data["default_instance"] = remaining[0] if remaining else None
        self._save_json(data)

    def list_all(self) -> Dict[str, Dict]:
        return self._load_json().get("instances", {})

    def get_default(self) -> Optional[str]:
        return self._load_json().get("default_instance")

    def set_default(self, instance: str):
        data = self._load_json()
        if instance not in data.get("instances", {}):
            raise ValueError(f"Instance {instance} not configured. Run 'snow add' first.")
        data["default_instance"] = instance
        self._save_json(data)

    def load_config(self, instance: Optional[str] = None) -> "Config":
        """Resolve credentials from env vars and config file; return a populated Config."""
        from .config import AuthType, Config

        instance = instance or os.environ.get("snow_instance")
        client_id = os.environ.get("snow_client_id")
        client_secret = os.environ.get("snow_client_secret")
        if bool(client_id) != bool(client_secret):
            raise ValueError("Set both snow_client_id and snow_client_secret, or neither.")
        if client_id:
            instance = instance or self.get_default()
            return Config(
                auth_type=AuthType.OAUTH,
                client_id=client_id,
                client_secret=client_secret,
                **self._paths_for(instance),
            )

        user = os.environ.get("snow_user")
        password = os.environ.get("snow_pwd")

        if not instance or not user or not password:
            data = self._load_json()
            if not instance:
                instance = data.get("default_instance")
            if instance and "instances" in data:
                inst_cfg = data["instances"].get(instance, {})
                if inst_cfg.get("auth") == "oauth" and not (user and password):
                    client_id = inst_cfg.get("client_id")
                    return Config(
                        auth_type=AuthType.OAUTH,
                        client_id=client_id,
                        client_secret=self._get_password_from_keyring(instance, self._secret_account(client_id))
                        or inst_cfg.get("client_secret"),
                        **self._paths_for(instance),
                    )
                if not user:
                    user = inst_cfg.get("user")
                if not password:
                    password = self._get_password_from_keyring(instance, user)
                    if not password:
                        password = inst_cfg.get("password")

        return Config(user=user, password=password, **self._paths_for(instance))

    def _paths_for(self, instance: Optional[str]) -> dict:
        """Return the instance and its derived paths as Config keyword arguments."""
        if not instance:
            return {"instance": None}
        return {
            "instance": instance,
            "cookie_file": self.cookie_file_for(instance),
            "tmp_dir": self.tmp_dir_for(instance),
            "token_cache_file": self.token_cache_file_for(instance),
        }

    # ---------- private I/O ----------

    def _load_json(self) -> dict:
        """Return config dict; {} if file is absent; {} + stderr warning if corrupted."""
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Warning: config file is corrupted and will be ignored ({e})", file=sys.stderr)
            return {}

    def _save_json(self, data: dict):
        self.snow_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.snow_dir, 0o700)
        fd = os.open(str(self.config_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))

    # ---------- keyring ----------

    @staticmethod
    def _secret_account(client_id: str) -> str:
        return f"oauth:{client_id}"

    def _delete_stored_secret(self, instance: str, entry: Optional[dict]):
        """Remove the keyring secret and any tokens belonging to an instance entry."""
        if not entry:
            return
        if entry.get("auth") == "oauth":
            client_id = entry.get("client_id")
            self._delete_password_from_keyring(instance, self._secret_account(client_id))
            TokenStore(instance, client_id, self.token_cache_file_for(instance)).clear()
        elif entry.get("user"):
            self._delete_password_from_keyring(instance, entry["user"])

    def _get_password_from_keyring(self, instance: str, user: str) -> Optional[str]:
        if not user:
            return None
        try:
            return keyring.get_password(f"snow-cli:{instance}", user)
        except Exception:
            return None

    def _set_password_in_keyring(self, instance: str, user: str, password: str) -> bool:
        try:
            keyring.set_password(f"snow-cli:{instance}", user, password)
            return True
        except Exception:
            return False

    def _delete_password_from_keyring(self, instance: str, user: str):
        try:
            keyring.delete_password(f"snow-cli:{instance}", user)
        except Exception:
            pass
