import unittest

from snow_cli.config import Config


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
