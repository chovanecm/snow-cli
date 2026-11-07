import http.cookiejar
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from click.testing import CliRunner

keyring_stub = types.SimpleNamespace(
    get_password=lambda *a, **kw: None,
    set_password=lambda *a, **kw: None,
    delete_password=lambda *a, **kw: None,
)
sys.modules.setdefault("keyring", keyring_stub)

from snow_cli.cli import main
from snow_cli.client import (
    DEFAULT_TIMEOUT,
    SCRIPT_TIMEOUT,
    ScriptTokenError,
    ServiceNowClient,
)
from snow_cli.commands import run_script
from snow_cli.config import Config


def _make_config(tmp_dir, instance="dev.example.com", user="admin", password="pass"):
    return Config(
        instance=instance,
        user=user,
        password=password,
        cookie_file=Path(tmp_dir) / "cookies.txt",
        tmp_dir=Path(tmp_dir),
    )


class RestSessionTests(unittest.TestCase):
    def test_rest_session_has_basic_auth_pre_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
        self.assertEqual(client._rest.auth, ("admin", "pass"))

    def test_rest_session_has_accept_json_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
        self.assertEqual(client._rest.headers.get("Accept"), "application/json")

    def test_scraping_session_is_none_before_first_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
        self.assertIsNone(client._scraping)

    def test_rest_get_calls_rest_session_with_correct_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
            mock_resp = Mock()
            with patch.object(client._rest, "get", return_value=mock_resp) as mock_get:
                result = client.rest_get("/api/now/table/incident", params={"sysparm_limit": "10"})
        mock_get.assert_called_once_with(
            "https://dev.example.com/api/now/table/incident",
            params={"sysparm_limit": "10"},
            headers={},
            timeout=DEFAULT_TIMEOUT,
        )
        self.assertIs(result, mock_resp)


class ScrapingSessionTests(unittest.TestCase):
    def test_scraping_session_created_on_first_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
            with patch.object(client, "_save_cookies"):
                with patch.object(client, "_get_scraping_session") as mock_init:
                    mock_session = Mock()
                    mock_session.get.return_value = Mock()
                    mock_init.return_value = mock_session
                    client.scraping_get("/login.do")
            mock_init.assert_called_once()

    def test_corrupted_cookie_file_prints_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            cookie_file = Path(tmp) / "cookies.txt"
            cookie_file.write_text("not a valid cookie jar file")
            config = Config(
                instance="dev.example.com",
                user="admin",
                password="pass",
                cookie_file=cookie_file,
            )
            client = ServiceNowClient(config)
            buf = io.StringIO()
            with redirect_stderr(buf):
                client._get_scraping_session()
        self.assertIn("Warning", buf.getvalue())

    def test_missing_cookie_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                instance="dev.example.com",
                user="admin",
                password="pass",
                cookie_file=Path(tmp) / "nonexistent.txt",
            )
            client = ServiceNowClient(config)
            client._get_scraping_session()  # must not raise

    def test_none_cookie_file_does_not_raise(self):
        config = Config(instance="dev.example.com", user="admin", password="pass")
        client = ServiceNowClient(config)
        client._get_scraping_session()  # must not raise


class TimeoutTests(unittest.TestCase):
    def _client_with_mock_scraping(self, tmp):
        client = ServiceNowClient(_make_config(tmp))
        client._scraping = Mock()
        return client

    def test_script_timeout_is_much_longer_than_default(self):
        self.assertGreaterEqual(SCRIPT_TIMEOUT[1], DEFAULT_TIMEOUT[1] * 10)

    def test_scraping_get_uses_default_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            with patch.object(client, "_save_cookies"):
                client.scraping_get("/login.do")
        client._scraping.get.assert_called_once_with(
            "https://dev.example.com/login.do", timeout=DEFAULT_TIMEOUT
        )

    def test_scraping_post_uses_default_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            with patch.object(client, "_save_cookies"):
                client.scraping_post("/login.do", data={"a": "b"})
        client._scraping.post.assert_called_once_with(
            "https://dev.example.com/login.do", data={"a": "b"}, timeout=DEFAULT_TIMEOUT
        )

    def test_scraping_get_caller_timeout_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            with patch.object(client, "_save_cookies"):
                client.scraping_get("/login.do", timeout=5)
        self.assertEqual(client._scraping.get.call_args.kwargs["timeout"], 5)

    def test_scraping_post_caller_timeout_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            with patch.object(client, "_save_cookies"):
                client.scraping_post("/login.do", timeout=5)
        self.assertEqual(client._scraping.post.call_args.kwargs["timeout"], 5)

    def test_script_post_uses_script_timeout_and_token_get_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            client._scraping.get.return_value = Mock(
                text='<input name="sysparm_ck" value="abc123">'
            )
            client._scraping.post.return_value = Mock(status_code=200, text="")
            config = _make_config(tmp)
            with patch("snow_cli.commands.ServiceNowClient", return_value=client), \
                 patch.object(client, "_save_cookies"), \
                 patch("snow_cli.commands._parse_and_display_output"):
                exit_code = run_script(config, script_content="gs.print('hi');")
        self.assertEqual(exit_code, 0)
        self.assertEqual(client._scraping.get.call_args.kwargs["timeout"], DEFAULT_TIMEOUT)
        self.assertEqual(client._scraping.post.call_args.kwargs["timeout"], SCRIPT_TIMEOUT)

    def test_script_read_timeout_warns_script_may_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._client_with_mock_scraping(tmp)
            client._scraping.get.return_value = Mock(
                text='<input name="sysparm_ck" value="abc123">'
            )
            client._scraping.post.side_effect = requests.exceptions.ReadTimeout("timed out")
            config = _make_config(tmp)
            buf = io.StringIO()
            with patch("snow_cli.commands.ServiceNowClient", return_value=client), \
                 patch.object(client, "_save_cookies"), \
                 redirect_stderr(buf):
                exit_code = run_script(config, script_content="gs.print('hi');")
        self.assertEqual(exit_code, 1)
        self.assertIn("may still be running", buf.getvalue())

    def test_cli_reports_timeout_without_traceback(self):
        cases = [
            ["run", "-"],
            ["r", "search", "incident"],
            ["r", "count", "incident"],
            ["table", "fields", "incident"],
        ]
        for args in cases:
            with self.subTest(args=args), tempfile.TemporaryDirectory() as tmp:
                client = self._client_with_mock_scraping(tmp)
                timeout = requests.exceptions.ConnectTimeout("connect timed out")
                client._scraping.get.side_effect = timeout
                client._rest = Mock()
                client._rest.get.side_effect = timeout
                repository = Mock()
                repository.load_config.return_value = _make_config(tmp)
                with patch("snow_cli.cli.InstanceRepository", return_value=repository), \
                     patch("snow_cli.commands.ServiceNowClient", return_value=client):
                    result = CliRunner().invoke(main, args, input="gs.print('hi');")
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIsInstance(result.exception, SystemExit)
                self.assertIn("connect timed out", result.output)
                self.assertNotIn("Traceback", result.output)


class TokenExtractionTests(unittest.TestCase):
    def test_extract_token_returns_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
        token = client._extract_token('value="abc123def"', r'value="([a-zA-Z0-9]+)"')
        self.assertEqual(token, "abc123def")

    def test_extract_token_returns_none_when_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
        self.assertIsNone(client._extract_token("no match here", r'value="([a-zA-Z0-9]+)"'))

    def test_get_script_token_raises_script_token_error_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
            mock_response = Mock()
            mock_response.text = "<html>no token here</html>"
            with patch.object(client, "scraping_get", return_value=mock_response):
                with self.assertRaises(ScriptTokenError):
                    client.get_script_token()

    def test_get_login_token_raises_value_error_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
            mock_response = Mock()
            mock_response.text = "<html>no token here</html>"
            with patch.object(client, "scraping_get", return_value=mock_response):
                with self.assertRaises(ValueError):
                    client.get_login_token()

    def test_get_elevate_token_raises_value_error_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ServiceNowClient(_make_config(tmp))
            mock_response = Mock()
            mock_response.text = "<html>no token here</html>"
            with patch.object(client, "scraping_get", return_value=mock_response):
                with self.assertRaises(ValueError):
                    client.get_elevate_token()


if __name__ == "__main__":
    unittest.main()
