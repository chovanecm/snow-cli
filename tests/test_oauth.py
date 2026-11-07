import contextlib
import io
import json
import socket
import sys
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import ANY, Mock, patch

import requests
from requests.adapters import HTTPAdapter

sys.modules.setdefault(
    "keyring",
    types.SimpleNamespace(
        get_password=lambda *a, **kw: None,
        set_password=lambda *a, **kw: None,
        delete_password=lambda *a, **kw: None,
    ),
)

from snow_cli.client import DEFAULT_TIMEOUT
from snow_cli.config import AuthType, Config
from snow_cli.oauth import (
    BearerAuth,
    CallbackServer,
    OAuthError,
    OAuthLoginRequired,
    OAuthRejected,
    OAuthTokenProvider,
    authorization_url,
    browser_login,
    exchange_code,
    refresh_tokens,
)
from snow_cli.token_store import Tokens

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "s3cret-value"
NOW = 1000.0


def _config():
    return Config(
        instance="dev.example.com",
        auth_type=AuthType.OAUTH,
        client_id="abc",
        client_secret=SECRET,
    )


def _now():
    return NOW


def _response(status=200, body=None, text=None, location=None):
    response = Mock()
    response.status_code = status
    response.is_redirect = 300 <= status < 400
    response.headers = {"Location": location} if location else {}
    if body is not None:
        response.json.return_value = body
    else:
        response.json.side_effect = ValueError("not json")
    response.text = text or ""
    return response


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


class TokenRequestTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("snow_cli.oauth.requests.post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.post.return_value = _response(body=_fixture("oauth_token_response.json"))

    def test_exchange_code_posts_form_fields_without_following_redirects(self):
        exchange_code(_config(), "the-code", now=_now)
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://dev.example.com/oauth_token.do")
        self.assertEqual(kwargs["data"], {
            "grant_type": "authorization_code",
            "code": "the-code",
            "redirect_uri": "http://localhost:8765/callback",
            "client_id": "abc",
            "client_secret": SECRET,
        })
        self.assertEqual(kwargs["timeout"], DEFAULT_TIMEOUT)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertNotIn("json", kwargs)

    def test_refresh_posts_form_fields_without_following_redirects(self):
        self.post.return_value = _response(body=_fixture("oauth_refresh_response.json"))
        refresh_tokens(_config(), "the-refresh", now=_now)
        kwargs = self.post.call_args[1]
        self.assertEqual(kwargs["data"], {
            "grant_type": "refresh_token",
            "refresh_token": "the-refresh",
            "client_id": "abc",
            "client_secret": SECRET,
        })
        self.assertEqual(kwargs["timeout"], DEFAULT_TIMEOUT)
        self.assertIs(kwargs["allow_redirects"], False)

    def test_parses_real_response_shape(self):
        tokens = exchange_code(_config(), "code", now=_now)
        self.assertEqual(tokens, Tokens("<access_token>", "<refresh_token>", NOW + 1799))

    def test_refresh_token_may_be_absent(self):
        self.post.return_value = _response(body={"access_token": "a", "expires_in": 10})
        self.assertIsNone(refresh_tokens(_config(), "r", now=_now).refresh_token)

    def test_missing_fields_raise(self):
        for body in ({"expires_in": 10}, {"access_token": "a"}, {"access_token": "a", "expires_in": "soon"}, []):
            with self.subTest(body=body):
                self.post.return_value = _response(body=body)
                with self.assertRaises(OAuthError):
                    exchange_code(_config(), "code", now=_now)

    def test_redirect_raises_and_sends_nothing_further(self):
        self.post.return_value = _response(status=307, location="https://evil.example.com/")
        with self.assertRaises(OAuthError) as caught:
            exchange_code(_config(), "code", now=_now)
        self.assertNotIsInstance(caught.exception, OAuthRejected)
        self.assertEqual(self.post.call_count, 1)

    def test_refused_request_reports_the_reason_without_the_secret(self):
        self.post.return_value = _response(
            status=401, body={"error": "server_error", "error_description": "access_denied"}
        )
        with self.assertRaises(OAuthRejected) as caught:
            exchange_code(_config(), "code", now=_now)
        self.assertIn("access_denied", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_refused_request_falls_back_to_error_field(self):
        self.post.return_value = _response(status=400, body={"error": "invalid_grant"})
        with self.assertRaisesRegex(OAuthRejected, "invalid_grant"):
            refresh_tokens(_config(), "r", now=_now)

    def test_non_json_response_reports_the_status(self):
        for status in (200, 503):
            with self.subTest(status=status):
                self.post.return_value = _response(status=status, text="<html>hibernating</html>")
                with self.assertRaisesRegex(OAuthError, str(status)) as caught:
                    exchange_code(_config(), "code", now=_now)
                self.assertNotIsInstance(caught.exception, OAuthRejected)

    def test_network_failure_is_an_oauth_error(self):
        self.post.side_effect = requests.ConnectionError("no route")
        with self.assertRaises(OAuthError) as caught:
            exchange_code(_config(), "code", now=_now)
        self.assertNotIsInstance(caught.exception, OAuthRejected)


class FakeStore:
    def __init__(self, tokens=None):
        self.tokens = tokens
        self.saved = []

    def load(self):
        return self.tokens

    def save(self, tokens):
        self.tokens = tokens
        self.saved.append(tokens)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("snow_cli.oauth.refresh_tokens")
        self.refresh = patcher.start()
        self.addCleanup(patcher.stop)
        self.refresh.return_value = Tokens("new", "new-refresh", NOW + 1800)

    def provider(self, tokens):
        self.store = FakeStore(tokens)
        return OAuthTokenProvider(_config(), self.store, now=_now)

    def test_valid_token_makes_no_request(self):
        provider = self.provider(Tokens("old", "r", NOW + 61))
        self.assertEqual(provider.get_token(), "old")
        self.refresh.assert_not_called()

    def test_token_near_expiry_is_refreshed_and_stored(self):
        provider = self.provider(Tokens("old", "r", NOW + 60))
        self.assertEqual(provider.get_token(), "new")
        self.refresh.assert_called_once_with(provider._config, "r", now=_now)
        self.assertEqual(self.store.saved, [Tokens("new", "new-refresh", NOW + 1800)])

    def test_force_refresh_ignores_a_valid_token(self):
        self.assertEqual(self.provider(Tokens("old", "r", NOW + 1000)).get_token(force_refresh=True), "new")

    def test_response_without_refresh_token_keeps_the_old_one(self):
        self.refresh.return_value = Tokens("new", None, NOW + 1800)
        self.provider(Tokens("old", "r", NOW)).get_token()
        self.assertEqual(self.store.tokens.refresh_token, "r")

    def test_no_stored_tokens_requires_login(self):
        with self.assertRaisesRegex(OAuthLoginRequired, "snow login"):
            self.provider(None).get_token()

    def test_expired_token_without_refresh_token_requires_login(self):
        with self.assertRaisesRegex(OAuthLoginRequired, "snow login"):
            self.provider(Tokens("old", None, NOW)).get_token()

    def test_rejected_refresh_requires_login(self):
        self.refresh.side_effect = OAuthRejected("expired")
        with self.assertRaisesRegex(OAuthLoginRequired, "snow login"):
            self.provider(Tokens("old", "r", NOW)).get_token()

    def test_network_failure_does_not_require_login(self):
        self.refresh.side_effect = OAuthError("no route")
        with self.assertRaises(OAuthError) as caught:
            self.provider(Tokens("old", "r", NOW)).get_token()
        self.assertNotIsInstance(caught.exception, OAuthLoginRequired)


class ScriptedAdapter(HTTPAdapter):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)
        self.auth_headers = []

    def send(self, request, **kwargs):
        self.auth_headers.append(request.headers.get("Authorization"))
        response = requests.Response()
        response.status_code = self.statuses.pop(0)
        response._content = b"{}"
        response.request = request
        response.url = request.url
        response.connection = self
        return response


class BearerAuthTests(unittest.TestCase):
    def get(self, statuses):
        self.adapter = ScriptedAdapter(statuses)
        self.provider = Mock()
        self.provider.get_token.side_effect = lambda force_refresh=False: "new" if force_refresh else "old"
        session = requests.Session()
        session.mount("https://", self.adapter)
        session.auth = BearerAuth(self.provider)
        return session.get("https://dev.example.com/api/now/table/incident")

    def test_sets_bearer_header(self):
        response = self.get([200])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.adapter.auth_headers, ["Bearer old"])
        self.provider.get_token.assert_called_once_with()

    def test_401_refreshes_and_retries_once(self):
        response = self.get([401, 200])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.adapter.auth_headers, ["Bearer old", "Bearer new"])
        self.assertEqual([r.status_code for r in response.history], [401])

    def test_second_401_is_returned_without_another_request(self):
        response = self.get([401, 401])
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.adapter.auth_headers), 2)

    def test_other_errors_are_not_retried(self):
        self.assertEqual(self.get([403]).status_code, 403)
        self.assertEqual(len(self.adapter.auth_headers), 1)


class AuthorizationUrlTests(unittest.TestCase):
    def test_contains_expected_parameters_and_no_secret(self):
        url = urllib.parse.urlsplit(authorization_url(_config(), "st&te"))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "dev.example.com", "/oauth_auth.do"))
        self.assertEqual(urllib.parse.parse_qs(url.query), {
            "response_type": ["code"],
            "client_id": ["abc"],
            "redirect_uri": ["http://localhost:8765/callback"],
            "state": ["st&te"],
        })
        self.assertNotIn(SECRET, url.query)


class CallbackServerTests(unittest.TestCase):
    STATE = "good-state"

    def setUp(self):
        self.server = CallbackServer(self.STATE, port=0)
        self.addCleanup(self.server.server_close)
        self.outcome = {}

    def wait_in_background(self, timeout=5):
        def run():
            try:
                self.outcome["code"] = self.server.wait_for_code(timeout)
            except OAuthError as e:
                self.outcome["error"] = e

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join)
        return thread

    def get(self, path):
        url = f"http://127.0.0.1:{self.server.server_address[1]}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.getcode(), response.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def query(self, **params):
        return "/callback?" + urllib.parse.urlencode(params)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_correct_callback_returns_the_code(self):
        thread = self.wait_in_background()
        status, _ = self.get(self.query(state=self.STATE, code="the-code"))
        thread.join()
        self.assertEqual(status, 200)
        self.assertEqual(self.outcome["code"], "the-code")

    def test_wrong_state_is_refused_and_waiting_continues(self):
        thread = self.wait_in_background()
        self.assertEqual(self.get(self.query(state="bad", code="evil"))[0], 400)
        self.assertEqual(self.get(self.query(code="evil"))[0], 400)
        self.assertEqual(self.get(self.query(state="bad", error="access_denied"))[0], 400)
        self.get(self.query(state=self.STATE, code="the-code"))
        thread.join()
        self.assertEqual(self.outcome, {"code": "the-code"})

    def test_other_paths_keep_waiting(self):
        thread = self.wait_in_background()
        self.assertEqual(self.get("/favicon.ico")[0], 404)
        self.get(self.query(state=self.STATE, code="the-code"))
        thread.join()
        self.assertEqual(self.outcome, {"code": "the-code"})

    def test_error_callback_raises_with_the_reason(self):
        thread = self.wait_in_background()
        self.get(self.query(state=self.STATE, error="access_denied", error_description="User said no"))
        thread.join()
        self.assertIn("User said no", str(self.outcome["error"]))

    def test_callback_without_code_raises(self):
        thread = self.wait_in_background()
        self.get(self.query(state=self.STATE))
        thread.join()
        self.assertIsInstance(self.outcome["error"], OAuthError)

    def test_response_does_not_echo_query_values(self):
        thread = self.wait_in_background()
        marker = "<script>marker</script>"
        for params in (
            {"state": marker, "code": marker},
            {"state": self.STATE, "error": marker, "error_description": marker},
        ):
            self.assertNotIn("marker", self.get(self.query(**params))[1])
        thread.join()

    def test_code_is_not_logged(self):
        thread = self.wait_in_background()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.get(self.query(state=self.STATE, code="the-code"))
            thread.join()
        self.assertEqual(stderr.getvalue(), "")

    def test_timeout_raises(self):
        with self.assertRaisesRegex(OAuthError, "Timed out"):
            self.server.wait_for_code(0.05)


class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        for target, name in (("snow_cli.oauth.webbrowser.open", "open_browser"), ("snow_cli.oauth.exchange_code", "exchange")):
            patcher = patch(target)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.exchange.return_value = Tokens("a", "r", NOW)
        self.store = FakeStore()

    def login(self, port=0):
        stdout = io.StringIO()
        with patch("snow_cli.oauth.REDIRECT_PORT", port), contextlib.redirect_stdout(stdout):
            browser_login(_config(), self.store)
        return stdout.getvalue()

    def test_stores_tokens_from_the_received_code(self):
        calls = []
        self.open_browser.side_effect = lambda url: calls.append("browser")
        with patch.object(CallbackServer, "wait_for_code", side_effect=lambda timeout: calls.append("wait") or "code"):
            output = self.login()
        self.assertEqual(calls, ["browser", "wait"])
        self.exchange.assert_called_once_with(ANY, "code")
        self.assertEqual(self.store.saved, [Tokens("a", "r", NOW)])
        url = self.open_browser.call_args[0][0]
        self.assertIn(url, output)
        self.assertNotIn(SECRET, output)

    def test_states_differ_between_logins(self):
        with patch.object(CallbackServer, "wait_for_code", return_value="code"):
            self.login()
            self.login()
        first, second = (urllib.parse.parse_qs(urllib.parse.urlsplit(c[0][0]).query)["state"][0]
                         for c in self.open_browser.call_args_list)
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_busy_port_fails_before_opening_the_browser(self):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            with self.assertRaisesRegex(OAuthError, str(port)):
                self.login(port=port)
        self.open_browser.assert_not_called()

    def test_failed_sign_in_stores_nothing(self):
        with patch.object(CallbackServer, "wait_for_code", side_effect=OAuthError("Timed out")):
            with self.assertRaises(OAuthError):
                self.login()
        self.assertEqual(self.store.saved, [])
