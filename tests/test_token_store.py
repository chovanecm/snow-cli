import contextlib
import io
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.modules.setdefault(
    "keyring",
    types.SimpleNamespace(
        get_password=lambda *a, **kw: None,
        set_password=lambda *a, **kw: None,
        delete_password=lambda *a, **kw: None,
    ),
)

from snow_cli.token_store import Tokens, TokenStore

SERVICE = "snow-cli:dev.example.com"
ACCOUNT = "oauth-token:abc"
TOKENS = Tokens("access", "refresh", 1000.0)


class FakeKeyring:
    def __init__(self, failing=False):
        self.values = {}
        self.failing = failing

    def _check(self):
        if self.failing:
            raise RuntimeError("no keyring")

    def get_password(self, service, account):
        self._check()
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self._check()
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self._check()
        del self.values[(service, account)]


class TokenStoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "instance" / "oauth_token.json"
        self.store = TokenStore("dev.example.com", "abc", self.file)

    def use_keyring(self, keyring):
        patcher = patch("snow_cli.token_store.keyring", keyring)
        patcher.start()
        self.addCleanup(patcher.stop)
        return keyring

    def save_quietly(self, tokens):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.store.save(tokens)
        return stderr.getvalue()


class KeyringTests(TokenStoreTestCase):
    def setUp(self):
        super().setUp()
        self.keyring = self.use_keyring(FakeKeyring())

    def test_round_trip_leaves_no_file(self):
        self.assertEqual(self.save_quietly(TOKENS), "")
        self.assertIn((SERVICE, ACCOUNT), self.keyring.values)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.store.load(), TOKENS)

    def test_token_without_refresh_token_round_trips(self):
        tokens = Tokens("access", None, 1000.0)
        self.store.save(tokens)
        self.assertEqual(self.store.load(), tokens)

    def test_nothing_stored_loads_as_none(self):
        self.assertIsNone(self.store.load())

    def test_corrupt_values_load_as_none(self):
        for value in ("not json", "[]", '{"access_token": "a"}', '{"access_token": "a", "expires_at": "soon"}'):
            with self.subTest(value=value):
                self.keyring.values[(SERVICE, ACCOUNT)] = value
                self.assertIsNone(self.store.load())

    def test_saving_to_keyring_removes_an_old_fallback_file(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("old")
        self.store.save(TOKENS)
        self.assertFalse(self.file.exists())

    def test_clear_removes_stored_tokens(self):
        self.store.save(TOKENS)
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_clear_with_nothing_stored_does_not_raise(self):
        self.store.clear()


class FallbackFileTests(TokenStoreTestCase):
    def setUp(self):
        super().setUp()
        self.use_keyring(FakeKeyring(failing=True))

    def test_round_trip_warns_and_uses_a_private_file(self):
        self.assertIn("Warning", self.save_quietly(TOKENS))
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(self.store.load(), TOKENS)

    def test_warning_contains_no_tokens(self):
        warning = self.save_quietly(TOKENS)
        self.assertNotIn("access", warning.replace("accessible", ""))
        self.assertNotIn("refresh", warning)

    def test_leaves_no_temp_files(self):
        self.save_quietly(TOKENS)
        self.assertEqual([p.name for p in self.file.parent.iterdir()], [self.file.name])

    def test_corrupt_file_loads_as_none(self):
        self.save_quietly(TOKENS)
        self.file.write_text("not json")
        self.assertIsNone(self.store.load())

    def test_clear_removes_the_file(self):
        self.save_quietly(TOKENS)
        self.store.clear()
        self.assertFalse(self.file.exists())
