"""Runtime configuration value object."""
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Bare hostname with optional port. The value is joined into URLs and file paths.
_INSTANCE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?")


class AuthType(str, Enum):
    BASIC = "basic"
    OAUTH = "oauth"

    def __str__(self) -> str:
        return self.value


@dataclass
class Config:
    """Runtime configuration for a ServiceNow instance."""
    instance: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    cookie_file: Optional[Path] = None
    tmp_dir: Optional[Path] = None
    auth_type: AuthType = AuthType.BASIC
    client_id: Optional[str] = None
    client_secret: Optional[str] = field(default=None, repr=False)
    token_cache_file: Optional[Path] = None

    @property
    def is_oauth(self) -> bool:
        return self.auth_type is AuthType.OAUTH

    def ensure_instance_set(self):
        if not self.instance:
            raise ValueError(
                "No instance specified. Use --instance flag or run 'snow use <instance>' to set default."
            )
        if not _INSTANCE_RE.fullmatch(self.instance):
            raise ValueError(
                f"Invalid instance {self.instance!r}. Expected a hostname such as dev1234.service-now.com."
            )

    def ensure_credentials_set(self):
        self.ensure_instance_set()
        if self.is_oauth:
            if not self.client_id or not self.client_secret:
                raise ValueError(
                    f"No OAuth credentials configured for {self.instance}. "
                    f"Run 'snow add {self.instance} --oauth' to configure."
                )
            return
        if not self.user or not self.password:
            raise ValueError(
                f"No credentials configured for {self.instance}. Run 'snow add {self.instance}' to configure."
            )
