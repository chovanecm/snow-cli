# tests/test_instance_repository.py
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

keyring_stub = types.SimpleNamespace(
    get_password=lambda *a, **kw: None,
    set_password=lambda *a, **kw: None,
    delete_password=lambda *a, **kw: None,
)
sys.modules.setdefault("keyring", keyring_stub)

from snow_cli.config import AuthType
from snow_cli.instance_repository import InstanceRepository
from snow_cli.token_store import Tokens, TokenStore


class LoadJsonTests(unittest.TestCase):
    def test_returns_empty_dict_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = InstanceRepository(snow_dir=Path(tmp) / ".snow-run")
            self.assertEqual(repo._load_json(), {})

    def test_returns_data_for_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            snow_dir = Path(tmp) / ".snow-run"
            snow_dir.mkdir()
            (snow_dir / "config.json").write_text(
                '{"default_instance": "dev.service-now.com"}'
            )
            repo = InstanceRepository(snow_dir=snow_dir)
            self.assertEqual(repo._load_json()["default_instance"], "dev.service-now.com")

    def test_returns_empty_dict_and_warns_for_corrupted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            snow_dir = Path(tmp) / ".snow-run"
            snow_dir.mkdir()
            (snow_dir / "config.json").write_text("{ not valid json }")
            repo = InstanceRepository(snow_dir=snow_dir)
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = repo._load_json()
            self.assertEqual(result, {})
            self.assertIn("Warning", buf.getvalue())


class PathHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InstanceRepository(snow_dir=Path(self._tmp.name) / ".snow-run")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cookie_file_for_returns_expected_path(self):
        expected = self.repo.snow_dir / "tmp" / "dev.service-now.com" / "cookies.txt"
        self.assertEqual(self.repo.cookie_file_for("dev.service-now.com"), expected)

    def test_tmp_dir_for_returns_expected_path(self):
        expected = self.repo.snow_dir / "tmp" / "dev.service-now.com"
        self.assertEqual(self.repo.tmp_dir_for("dev.service-now.com"), expected)


class InstanceRepositoryCrudTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InstanceRepository(snow_dir=Path(self._tmp.name) / ".snow-run")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_config(self, data: dict):
        self.repo.snow_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.snow_dir / "config.json").write_text(json.dumps(data))

    def test_save_persists_instance_and_user_to_file(self):
        self.repo.save("dev.service-now.com", "admin", "pass123")
        data = self.repo._load_json()
        self.assertIn("dev.service-now.com", data["instances"])
        self.assertEqual(data["instances"]["dev.service-now.com"]["user"], "admin")

    def test_save_sets_default_when_no_existing_default(self):
        self.repo.save("dev.service-now.com", "admin", "pass123")
        self.assertEqual(self.repo.get_default(), "dev.service-now.com")

    def test_save_does_not_overwrite_existing_default(self):
        self.repo.save("dev1.service-now.com", "admin", "pass1")
        self.repo.save("dev2.service-now.com", "admin", "pass2")
        self.assertEqual(self.repo.get_default(), "dev1.service-now.com")

    def test_save_with_set_default_true_changes_default(self):
        self.repo.save("dev1.service-now.com", "admin", "pass1")
        self.repo.save("dev2.service-now.com", "admin", "pass2", set_default=True)
        self.assertEqual(self.repo.get_default(), "dev2.service-now.com")

    def test_save_stores_password_in_file_when_keyring_unavailable(self):
        failing_keyring = types.SimpleNamespace(
            get_password=lambda *a, **kw: None,
            set_password=lambda *a, **kw: (_ for _ in ()).throw(Exception("no keyring")),
            delete_password=lambda *a, **kw: None,
        )
        with patch.dict(sys.modules, {"keyring": failing_keyring}):
            import importlib
            import snow_cli.instance_repository as ir_mod
            importlib.reload(ir_mod)
            repo = ir_mod.InstanceRepository(snow_dir=Path(self._tmp.name) / ".snow-run-kr")
            repo.save("dev.service-now.com", "admin", "pass123")
            data = repo._load_json()
        importlib.reload(ir_mod)  # restore module state
        self.assertEqual(data["instances"]["dev.service-now.com"].get("password"), "pass123")
        self.assertFalse(data["instances"]["dev.service-now.com"]["keyring"])

    def test_list_all_returns_all_instances(self):
        self.repo.save("dev1.service-now.com", "admin", "pass1")
        self.repo.save("dev2.service-now.com", "admin", "pass2")
        instances = self.repo.list_all()
        self.assertIn("dev1.service-now.com", instances)
        self.assertIn("dev2.service-now.com", instances)

    def test_list_all_returns_empty_dict_when_no_config(self):
        self.assertEqual(self.repo.list_all(), {})

    def test_get_default_returns_none_when_no_config(self):
        self.assertIsNone(self.repo.get_default())

    def test_set_default_changes_default(self):
        self.repo.save("dev1.service-now.com", "admin", "pass1")
        self.repo.save("dev2.service-now.com", "admin", "pass2")
        self.repo.set_default("dev2.service-now.com")
        self.assertEqual(self.repo.get_default(), "dev2.service-now.com")

    def test_set_default_raises_for_unknown_instance(self):
        with self.assertRaises(ValueError):
            self.repo.set_default("unknown.service-now.com")

    def test_remove_deletes_instance(self):
        self.repo.save("dev.service-now.com", "admin", "pass")
        self.repo.remove("dev.service-now.com")
        self.assertNotIn("dev.service-now.com", self.repo.list_all())

    def test_remove_rotates_default_to_next_instance(self):
        self.repo.save("dev1.service-now.com", "admin", "pass1")
        self.repo.save("dev2.service-now.com", "admin", "pass2")
        self.repo.remove("dev1.service-now.com")
        self.assertEqual(self.repo.get_default(), "dev2.service-now.com")

    def test_remove_sets_default_to_none_when_last_instance_removed(self):
        self.repo.save("dev.service-now.com", "admin", "pass")
        self.repo.remove("dev.service-now.com")
        self.assertIsNone(self.repo.get_default())

    def test_remove_is_noop_for_nonexistent_instance(self):
        self.repo.save("dev.service-now.com", "admin", "pass")
        self.repo.remove("other.service-now.com")  # must not raise
        self.assertIn("dev.service-now.com", self.repo.list_all())


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = InstanceRepository(snow_dir=Path(self._tmp.name) / ".snow-run")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_config(self, data: dict):
        self.repo.snow_dir.mkdir(parents=True, exist_ok=True)
        (self.repo.snow_dir / "config.json").write_text(json.dumps(data))

    def test_returns_none_fields_when_nothing_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            config = self.repo.load_config()
        self.assertIsNone(config.instance)
        self.assertIsNone(config.user)
        self.assertIsNone(config.password)
        self.assertIsNone(config.cookie_file)
        self.assertIsNone(config.tmp_dir)

    def test_instance_arg_takes_priority_over_env(self):
        with patch.dict(os.environ, {"snow_instance": "env.service-now.com"}):
            config = self.repo.load_config(instance="arg.service-now.com")
        self.assertEqual(config.instance, "arg.service-now.com")

    def test_resolves_instance_from_snow_instance_env_var(self):
        with patch.dict(os.environ, {"snow_instance": "env.service-now.com"},
                        clear=False):
            config = self.repo.load_config()
        self.assertEqual(config.instance, "env.service-now.com")

    def test_resolves_instance_from_file_default_instance(self):
        self._write_config({
            "default_instance": "file.service-now.com",
            "instances": {"file.service-now.com": {"user": "admin", "keyring": False, "password": "p"}}
        })
        config = self.repo.load_config()
        self.assertEqual(config.instance, "file.service-now.com")

    def test_computes_cookie_file_and_tmp_dir_when_instance_known(self):
        self._write_config({
            "default_instance": "dev.service-now.com",
            "instances": {"dev.service-now.com": {"user": "admin", "keyring": False, "password": "p"}}
        })
        config = self.repo.load_config()
        self.assertEqual(config.cookie_file, self.repo.cookie_file_for("dev.service-now.com"))
        self.assertEqual(config.tmp_dir, self.repo.tmp_dir_for("dev.service-now.com"))

    def test_cookie_file_and_tmp_dir_are_none_when_no_instance(self):
        config = self.repo.load_config()
        self.assertIsNone(config.cookie_file)
        self.assertIsNone(config.tmp_dir)

    def test_credentials_from_env_override_file(self):
        self._write_config({
            "default_instance": "dev.service-now.com",
            "instances": {"dev.service-now.com": {"user": "file_user", "keyring": False, "password": "file_pass"}}
        })
        with patch.dict(os.environ, {"snow_user": "env_user", "snow_pwd": "env_pass"}, clear=False):
            config = self.repo.load_config()
        self.assertEqual(config.user, "env_user")
        self.assertEqual(config.password, "env_pass")

    def test_resolves_user_and_password_from_file(self):
        self._write_config({
            "default_instance": "dev.service-now.com",
            "instances": {
                "dev.service-now.com": {"user": "admin", "keyring": False, "password": "s3cr3t"}
            }
        })
        config = self.repo.load_config()
        self.assertEqual(config.user, "admin")
        self.assertEqual(config.password, "s3cr3t")


class OAuthEnvResolutionTests(unittest.TestCase):
    INSTANCE = "dev.service-now.com"
    OAUTH_ENV = {"snow_client_id": "abc", "snow_client_secret": "s3cret"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = InstanceRepository(snow_dir=Path(tmp.name) / ".snow-run")
        self.repo.snow_dir.mkdir()
        self.repo.config_file.write_text(json.dumps({
            "default_instance": self.INSTANCE,
            "instances": {self.INSTANCE: {"user": "stored", "keyring": False, "password": "stored_pass"}},
        }))

    def _load(self, env, instance=None):
        with patch.dict(os.environ, env, clear=True):
            return self.repo.load_config(instance=instance)

    def test_env_client_credentials_select_oauth_over_stored_basic(self):
        config = self._load(self.OAUTH_ENV)
        self.assertIs(config.auth_type, AuthType.OAUTH)
        self.assertEqual(config.client_id, "abc")
        self.assertEqual(config.client_secret, "s3cret")
        self.assertEqual(config.instance, self.INSTANCE)
        self.assertIsNone(config.user)
        self.assertIsNone(config.password)

    def test_env_client_credentials_win_over_env_user_and_password(self):
        config = self._load({**self.OAUTH_ENV, "snow_user": "env_user", "snow_pwd": "env_pass"})
        self.assertTrue(config.is_oauth)
        self.assertIsNone(config.password)

    def test_env_client_credentials_work_with_no_stored_instance(self):
        repo = InstanceRepository(snow_dir=self.repo.snow_dir / "empty")
        with patch.dict(os.environ, {**self.OAUTH_ENV, "snow_instance": "env.service-now.com"}, clear=True):
            config = repo.load_config()
        self.assertTrue(config.is_oauth)
        self.assertEqual(config.instance, "env.service-now.com")

    def test_only_one_client_variable_is_an_error(self):
        for name, value in self.OAUTH_ENV.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "snow_client_id and snow_client_secret"):
                    self._load({name: value})

    def test_error_for_partial_env_does_not_contain_the_secret(self):
        with self.assertRaises(ValueError) as caught:
            self._load({"snow_client_secret": "s3cret"})
        self.assertNotIn("s3cret", str(caught.exception))

    def test_stored_basic_is_unchanged_without_client_variables(self):
        config = self._load({})
        self.assertIs(config.auth_type, AuthType.BASIC)
        self.assertEqual(config.user, "stored")
        self.assertEqual(config.password, "stored_pass")

    def test_paths_are_set_for_both_auth_types(self):
        for env in ({}, self.OAUTH_ENV):
            with self.subTest(oauth=bool(env)):
                config = self._load(env)
                tmp_dir = self.repo.tmp_dir_for(self.INSTANCE)
                self.assertEqual(config.token_cache_file, tmp_dir / "oauth_token.json")
                self.assertEqual(config.cookie_file, self.repo.cookie_file_for(self.INSTANCE))
                self.assertEqual(config.tmp_dir, tmp_dir)


class DictKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        del self.values[(service, account)]


class StoredOAuthTests(unittest.TestCase):
    INSTANCE = "dev.service-now.com"
    SECRET_KEY = ("snow-cli:dev.service-now.com", "oauth:abc")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = InstanceRepository(snow_dir=Path(tmp.name) / ".snow-run")
        self.keyring = DictKeyring()
        for target in ("snow_cli.instance_repository.keyring", "snow_cli.token_store.keyring"):
            patcher = patch(target, self.keyring)
            patcher.start()
            self.addCleanup(patcher.stop)
        env_patcher = patch.dict(os.environ, {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def store_tokens(self, client_id="abc"):
        store = TokenStore(self.INSTANCE, client_id, self.repo.token_cache_file_for(self.INSTANCE))
        store.save(Tokens("access", "refresh", 1000.0))
        return store

    def test_save_oauth_keeps_the_secret_out_of_json(self):
        self.assertTrue(self.repo.save_oauth(self.INSTANCE, "abc", "s3cret"))
        self.assertEqual(self.keyring.values[self.SECRET_KEY], "s3cret")
        self.assertNotIn("s3cret", self.repo.config_file.read_text())
        self.assertEqual(
            self.repo.list_all()[self.INSTANCE],
            {"auth": "oauth", "client_id": "abc", "keyring": True},
        )

    def test_keyring_failure_stores_the_secret_in_json(self):
        with patch.object(self.keyring, "set_password", side_effect=RuntimeError("no keyring")):
            self.assertFalse(self.repo.save_oauth(self.INSTANCE, "abc", "s3cret"))
        self.assertEqual(self.repo.list_all()[self.INSTANCE]["client_secret"], "s3cret")
        self.assertEqual(self.repo.load_config().client_secret, "s3cret")

    def test_first_instance_becomes_default(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        self.assertEqual(self.repo.get_default(), self.INSTANCE)

    def test_load_config_returns_stored_oauth(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        config = self.repo.load_config()
        self.assertIs(config.auth_type, AuthType.OAUTH)
        self.assertEqual(config.instance, self.INSTANCE)
        self.assertEqual(config.client_id, "abc")
        self.assertEqual(config.client_secret, "s3cret")
        self.assertEqual(config.token_cache_file, self.repo.token_cache_file_for(self.INSTANCE))
        config.ensure_credentials_set()

    def test_env_user_and_password_override_stored_oauth(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        with patch.dict(os.environ, {"snow_user": "env_user", "snow_pwd": "env_pass"}):
            config = self.repo.load_config(instance=self.INSTANCE)
        self.assertIs(config.auth_type, AuthType.BASIC)
        self.assertEqual(config.user, "env_user")

    def test_only_one_of_env_user_and_password_leaves_stored_oauth(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        with patch.dict(os.environ, {"snow_user": "env_user"}):
            self.assertTrue(self.repo.load_config().is_oauth)

    def test_switching_to_basic_removes_secret_and_tokens(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        store = self.store_tokens()
        self.repo.save(self.INSTANCE, "admin", "pass")
        self.assertNotIn(self.SECRET_KEY, self.keyring.values)
        self.assertIsNone(store.load())
        self.assertFalse(self.repo.load_config().is_oauth)

    def test_switching_to_oauth_removes_the_password(self):
        self.repo.save(self.INSTANCE, "admin", "pass")
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        self.assertNotIn(("snow-cli:dev.service-now.com", "admin"), self.keyring.values)

    def test_switching_client_removes_the_old_secret_and_tokens(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        store = self.store_tokens()
        self.repo.save_oauth(self.INSTANCE, "xyz", "other")
        self.assertNotIn(self.SECRET_KEY, self.keyring.values)
        self.assertIsNone(store.load())

    def test_remove_deletes_secret_and_tokens(self):
        self.repo.save_oauth(self.INSTANCE, "abc", "s3cret")
        store = self.store_tokens()
        self.repo.remove(self.INSTANCE)
        self.assertEqual(self.keyring.values, {})
        self.assertIsNone(store.load())
        self.assertEqual(self.repo.list_all(), {})
