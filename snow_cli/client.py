"""Single HTTP entry point for all ServiceNow communication."""
import http.cookiejar
import re
import sys
from typing import Optional

import requests

from .config import Config

# requests timeouts as (connect, read) seconds. The read timeout limits the wait
# for each chunk of data, not the whole call.
CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, 60)
# /sys.scripts.do sends nothing until the script ends, so reads must outlast it.
SCRIPT_TIMEOUT = (CONNECT_TIMEOUT, 3600)


class ScriptTokenError(ValueError):
    """Raised when the background-script execution token cannot be obtained."""


class ServiceNowClient:
    """Facade owning both HTTP transports (cookie-auth scraping + basic-auth REST)."""

    def __init__(self, config: Config):
        self._base_url = f"https://{config.instance}"
        self._cookie_file = config.cookie_file

        self._rest = requests.Session()
        self._rest.auth = (config.user, config.password)
        self._rest.headers.update({"Accept": "application/json"})

        self._scraping: Optional[requests.Session] = None

    # ── REST transport ─────────────────────────────────────────────────────

    def rest_get(self, path: str, params=None, extra_headers=None) -> requests.Response:
        headers = dict(extra_headers) if extra_headers else {}
        return self._rest.get(
            f"{self._base_url}{path}",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )

    # ── Scraping transport ─────────────────────────────────────────────────

    def scraping_get(self, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        session = self._get_scraping_session()
        response = session.get(f"{self._base_url}{path}", **kwargs)
        self._save_cookies()
        return response

    def scraping_post(self, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        session = self._get_scraping_session()
        response = session.post(f"{self._base_url}{path}", **kwargs)
        self._save_cookies()
        return response

    def get_login_token(self) -> str:
        response = self.scraping_get("/login.do")
        token = self._extract_token(
            response.text, r'sysparm_ck[^>]*value="([a-zA-Z0-9_]+)"'
        )
        if not token:
            raise ValueError(f"Could not obtain login token from {self._base_url}")
        return token

    def get_script_token(self) -> str:
        response = self.scraping_get("/sys.scripts.do")
        token = self._extract_token(
            response.text, r'sysparm_ck[^>]*value="([a-zA-Z0-9_]+)"'
        )
        if not token:
            raise ScriptTokenError(
                f"Cannot get security token for {self._base_url}. "
                "Try logging in again (snow login)"
            )
        return token

    def get_elevate_token(self) -> str:
        response = self.scraping_get("/navpage.do")
        token = self._extract_token(response.text, r"g_ck = '([a-zA-Z0-9_]+)'")
        if not token:
            raise ValueError(
                "Could not obtain authentication token to elevate privileges"
            )
        return token

    # ── Private ────────────────────────────────────────────────────────────

    def _get_scraping_session(self) -> requests.Session:
        if self._scraping is None:
            self._scraping = requests.Session()
            self._load_cookies()
        return self._scraping

    def reload_cookies(self) -> None:
        """Re-read cookie file into an already-open scraping session."""
        if self._scraping is not None:
            self._load_cookies()

    def _load_cookies(self):
        if self._cookie_file is None or not self._cookie_file.exists():
            return
        try:
            jar = http.cookiejar.MozillaCookieJar(str(self._cookie_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            for cookie in jar:
                self._scraping.cookies.set_cookie(cookie)
        except Exception as e:
            print(
                f"Warning: cookie file could not be loaded and will be ignored ({e})",
                file=sys.stderr,
            )

    def _save_cookies(self):
        if self._cookie_file is None:
            return
        self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
        jar = http.cookiejar.MozillaCookieJar(str(self._cookie_file))
        for cookie in self._scraping.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True, ignore_expires=True)
        self._cookie_file.chmod(0o600)

    def _extract_token(self, html: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, html)
        return match.group(1) if match else None
