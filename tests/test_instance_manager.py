import contextlib
import io
import os
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

from click.testing import CliRunner

from snow_cli import cli as cli_module
from snow_cli.instance_manager import add_oauth_instance, list_instances, show_info
from snow_cli.instance_repository import InstanceRepository

INSTANCE = "dev.service-now.com"
SECRET = "s3cret-value"


class DictKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        del self.values[(service, account)]


class InstanceManagerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = InstanceRepository(snow_dir=Path(tmp.name) / ".snow-run")
        self.keyring = DictKeyring()
        for patcher in (
            patch("snow_cli.instance_repository.keyring", self.keyring),
            patch("snow_cli.token_store.keyring", self.keyring),
            patch("snow_cli.instance_manager.InstanceRepository", return_value=self.repo),
            patch.dict(os.environ, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def capture(self, fn, *args, **kwargs):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = fn(*args, **kwargs)
        return code, stdout.getvalue(), stderr.getvalue()

    def add(self, *args, secret=SECRET, typed_id="abc", **kwargs):
        with patch("builtins.input", return_value=typed_id) as prompt, \
             patch("snow_cli.instance_manager.getpass.getpass", return_value=secret) as getpass:
            result = self.capture(add_oauth_instance, *args, **kwargs)
        self.prompt, self.getpass = prompt, getpass
        return result


class AddOAuthInstanceTests(InstanceManagerTestCase):
    def test_prompts_for_client_id_and_always_for_the_secret(self):
        code, stdout, stderr = self.add(INSTANCE)
        self.assertEqual(code, 0)
        self.getpass.assert_called_once()
        self.assertEqual(self.repo.list_all()[INSTANCE]["client_id"], "abc")
        self.assertNotIn(SECRET, stdout + stderr)
        self.assertIn(f"snow -i {INSTANCE} login", stdout)

    def test_supplied_client_id_is_not_prompted_for(self):
        self.add(INSTANCE, client_id="given")
        self.prompt.assert_not_called()
        self.getpass.assert_called_once()
        self.assertEqual(self.repo.list_all()[INSTANCE]["client_id"], "given")

    def test_empty_values_are_refused(self):
        for kwargs in ({"typed_id": ""}, {"secret": ""}):
            with self.subTest(kwargs=kwargs):
                code, _, _ = self.add(INSTANCE, **kwargs)
                self.assertEqual(code, 1)
                self.assertEqual(self.repo.list_all(), {})

    def test_invalid_instance_is_refused(self):
        code, _, stderr = self.add("evil.example.com/../x")
        self.assertEqual(code, 1)
        self.assertIn("Invalid instance", stderr)
        self.assertEqual(self.repo.list_all(), {})

    def test_set_default(self):
        self.repo.save("other.service-now.com", "admin", "pass")
        self.add(INSTANCE, set_default=True)
        self.assertEqual(self.repo.get_default(), INSTANCE)

    def test_keyring_failure_warns(self):
        with patch.object(self.keyring, "set_password", side_effect=RuntimeError("no keyring")):
            code, stdout, _ = self.add(INSTANCE)
        self.assertEqual(code, 0)
        self.assertIn("Warning", stdout)


class DisplayTests(InstanceManagerTestCase):
    def test_list_handles_oauth_entries(self):
        self.repo.save_oauth(INSTANCE, "abc", SECRET)
        self.repo.save("basic.service-now.com", "admin", "pass")
        code, stdout, _ = self.capture(list_instances)
        self.assertEqual(code, 0)
        self.assertIn("Auth: OAuth authorization code", stdout)
        self.assertIn("Client ID: abc", stdout)
        self.assertIn("Client secret: Stored in [keyring]", stdout)
        self.assertIn("User: admin", stdout)
        self.assertNotIn(SECRET, stdout)

    def test_info_shows_oauth_state(self):
        self.repo.save_oauth(INSTANCE, "abc", SECRET)
        _, stdout, _ = self.capture(show_info)
        self.assertIn("Auth: OAuth authorization code", stdout)
        self.assertIn("Client secret: (set)", stdout)
        self.assertNotIn(SECRET, stdout)

    def test_info_for_basic_is_unchanged(self):
        self.repo.save(INSTANCE, "admin", "pass")
        _, stdout, _ = self.capture(show_info)
        self.assertIn("User: admin", stdout)
        self.assertIn("Password: (set)", stdout)
        self.assertNotIn("Auth:", stdout)


class AddCommandTests(unittest.TestCase):
    def test_oauth_flag_dispatches_to_add_oauth_instance(self):
        with patch("snow_cli.cli.InstanceRepository"), \
             patch("snow_cli.cli.add_oauth_instance", return_value=0) as add_oauth, \
             patch("snow_cli.cli.add_instance") as add_basic:
            result = CliRunner().invoke(
                cli_module.main, ["add", "--oauth", "--client-id", "abc", "--default", INSTANCE]
            )
        self.assertEqual(result.exit_code, 0)
        add_oauth.assert_called_once_with(INSTANCE, "abc", True)
        add_basic.assert_not_called()

    def test_client_id_needs_oauth(self):
        with patch("snow_cli.cli.InstanceRepository"), patch("snow_cli.cli.add_instance") as add_basic:
            result = CliRunner().invoke(cli_module.main, ["add", "--client-id", "abc", INSTANCE])
        self.assertNotEqual(result.exit_code, 0)
        add_basic.assert_not_called()

    def test_there_is_no_client_secret_option(self):
        with patch("snow_cli.cli.InstanceRepository"):
            result = CliRunner().invoke(cli_module.main, ["add", "--client-secret", "x", INSTANCE])
        self.assertNotEqual(result.exit_code, 0)
