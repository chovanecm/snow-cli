"""OAuth authorization code grant: token requests and bearer auth for requests."""
import time
from typing import Callable

import requests

from .config import Config
from .timeouts import DEFAULT_TIMEOUT
from .token_store import Tokens, TokenStore

REDIRECT_URI = "http://localhost:8765/callback"
EXPIRY_MARGIN_SECONDS = 60


class OAuthError(RuntimeError):
    """An OAuth request failed."""


class OAuthRejected(OAuthError):
    """The token endpoint refused the request."""


class OAuthLoginRequired(OAuthError):
    """No usable tokens remain, so the user has to sign in again."""


def exchange_code(config: Config, code: str, now: Callable[[], float] = time.time) -> Tokens:
    return _request_tokens(
        config,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        now,
    )


def refresh_tokens(config: Config, refresh_token: str, now: Callable[[], float] = time.time) -> Tokens:
    return _request_tokens(config, {"grant_type": "refresh_token", "refresh_token": refresh_token}, now)


def _request_tokens(config: Config, form: dict, now: Callable[[], float]) -> Tokens:
    url = f"https://{config.instance}/oauth_token.do"
    try:
        # Never follow redirects: on 307 and 308 the secret would go to the target.
        response = requests.post(
            url,
            data={**form, "client_id": config.client_id, "client_secret": config.client_secret},
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as e:
        raise OAuthError(f"Token request to {config.instance} failed: {e}") from e

    status = response.status_code
    if 300 <= status < 400:
        raise OAuthError(f"Token request to {config.instance} was redirected (HTTP {status})")
    try:
        data = response.json()
    except ValueError:
        raise OAuthError(f"Token request to {config.instance} returned HTTP {status} without JSON") from None
    if not isinstance(data, dict):
        raise OAuthError(f"Token request to {config.instance} returned an unexpected response")
    if status != 200:
        reason = data.get("error_description") or data.get("error") or f"HTTP {status}"
        error_type = OAuthRejected if 400 <= status < 500 else OAuthError
        raise error_type(f"Token request to {config.instance} was refused: {reason}")
    try:
        return Tokens(data["access_token"], data.get("refresh_token"), now() + float(data["expires_in"]))
    except (KeyError, TypeError, ValueError):
        raise OAuthError(f"Token response from {config.instance} is missing required fields") from None


class OAuthTokenProvider:
    """Supplies a valid access token, refreshing it when needed."""

    def __init__(self, config: Config, store: TokenStore, now: Callable[[], float] = time.time):
        self._config = config
        self._store = store
        self._now = now

    def get_token(self, force_refresh: bool = False) -> str:
        tokens = self._store.load()
        if tokens is None:
            raise self._login_required()
        if not force_refresh and tokens.expires_at - self._now() > EXPIRY_MARGIN_SECONDS:
            return tokens.access_token
        if not tokens.refresh_token:
            raise self._login_required()
        try:
            refreshed = refresh_tokens(self._config, tokens.refresh_token, now=self._now)
        except OAuthRejected as e:
            raise self._login_required() from e
        if refreshed.refresh_token is None:
            refreshed = refreshed._replace(refresh_token=tokens.refresh_token)
        self._store.save(refreshed)
        return refreshed.access_token

    def _login_required(self) -> OAuthLoginRequired:
        return OAuthLoginRequired(f"Not signed in to {self._config.instance}. Run 'snow login'.")


class BearerAuth(requests.auth.AuthBase):
    """Adds the bearer token, and on a 401 refreshes it and retries once."""

    def __init__(self, provider: OAuthTokenProvider):
        self._provider = provider

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
        request.register_hook("response", self._retry_on_401)
        return request

    def _retry_on_401(self, response, **kwargs):
        if response.status_code != 401:
            return response
        retry = response.request.copy()
        retry.headers["Authorization"] = f"Bearer {self._provider.get_token(force_refresh=True)}"
        # copy() shares the hooks with the original request; removing ours limits this to one retry.
        retry.deregister_hook("response", self._retry_on_401)
        response.content  # release the connection
        response.close()
        retried = response.connection.send(retry, **kwargs)
        retried.history.append(response)
        retried.request = retry
        return retried
