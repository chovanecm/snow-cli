import unittest

from snow_cli.config import AuthType, Config


class InstanceValidationTests(unittest.TestCase):
    def test_accepts_hostnames(self):
        for instance in ("dev1234.service-now.com", "localhost", "localhost:8080"):
            with self.subTest(instance=instance):
                Config(instance=instance).ensure_instance_set()

    def test_rejects_values_that_are_not_bare_hostnames(self):
        for instance in (
            "../../../x",
            "evil.example.com/../../../x",
            "evil.example.com/",
            "user@evil.example.com",
            "evil.example.com?x=1",
            "evil.example.com#x",
            "dev example.com",
            "dev.example.com\n",
            "-dev.example.com",
        ):
            with self.subTest(instance=instance):
                with self.assertRaisesRegex(ValueError, "Invalid instance"):
                    Config(instance=instance).ensure_instance_set()

    def test_missing_instance_message_is_unchanged(self):
        with self.assertRaisesRegex(ValueError, "No instance specified"):
            Config().ensure_instance_set()

    def test_credentials_check_validates_instance(self):
        config = Config(instance="evil.example.com/../x", user="admin", password="pass")
        with self.assertRaisesRegex(ValueError, "Invalid instance"):
            config.ensure_credentials_set()


class AuthTypeTests(unittest.TestCase):
    INSTANCE = "dev.example.com"

    def test_defaults_to_basic(self):
        config = Config(instance=self.INSTANCE)
        self.assertIs(config.auth_type, AuthType.BASIC)
        self.assertFalse(config.is_oauth)

    def test_oauth_config_needs_no_user(self):
        config = Config(
            instance=self.INSTANCE,
            auth_type=AuthType.OAUTH,
            client_id="abc",
            client_secret="s3cret",
        )
        self.assertTrue(config.is_oauth)
        config.ensure_credentials_set()

    def test_oauth_config_requires_client_id_and_secret(self):
        for values in ({"client_id": "abc"}, {"client_secret": "s3cret"}, {}):
            with self.subTest(values=sorted(values)):
                config = Config(instance=self.INSTANCE, auth_type=AuthType.OAUTH, **values)
                with self.assertRaisesRegex(ValueError, "snow add dev.example.com --oauth"):
                    config.ensure_credentials_set()

    def test_user_and_password_do_not_satisfy_oauth(self):
        config = Config(instance=self.INSTANCE, auth_type=AuthType.OAUTH, user="admin", password="pass")
        with self.assertRaises(ValueError):
            config.ensure_credentials_set()

    def test_basic_credentials_message_is_unchanged(self):
        with self.assertRaisesRegex(ValueError, r"Run 'snow add dev.example.com' to configure"):
            Config(instance=self.INSTANCE).ensure_credentials_set()

    def test_repr_hides_secrets(self):
        text = repr(Config(instance=self.INSTANCE, client_secret="s3cret", password="p4ss"))
        self.assertNotIn("s3cret", text)
        self.assertNotIn("p4ss", text)
