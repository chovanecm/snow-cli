import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.modules.setdefault(
    "keyring",
    types.SimpleNamespace(
        get_password=lambda *a, **kw: None,
        set_password=lambda *a, **kw: None,
        delete_password=lambda *a, **kw: None,
    ),
)


# Stub mcp so mcp_server can be imported in environments without the mcp package
class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def run(self):
        pass


sys.modules.setdefault("mcp", types.ModuleType("mcp"))
sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
sys.modules.setdefault("mcp.server.fastmcp", types.SimpleNamespace(FastMCP=_FakeFastMCP))
sys.modules.setdefault("mcp.types", types.SimpleNamespace(ToolAnnotations=Mock))

import snow_cli.mcp_server as mcp_server
from snow_cli.audit import _redact
from snow_cli.config import Config

INSTANCE = "dev.example.com"


class McpToolTestCase(unittest.TestCase):
    def setUp(self):
        self.config = Config(instance=INSTANCE, user="admin", password="pass")
        self.repository = Mock()
        self.repository.load_config.return_value = self.config
        for name, replacement in (
            ("InstanceRepository", Mock(return_value=self.repository)),
            ("log_tool_call", Mock()),
        ):
            patcher = patch.object(mcp_server, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.log_tool_call = mcp_server.log_tool_call


class CredentialLoadingTests(McpToolTestCase):
    def test_tools_pass_config_from_repository_to_commands(self):
        cases = [
            ("run_script", 0, lambda: mcp_server.snow_run_script("gs.print(1);", instance=INSTANCE)),
            ("login", 0, lambda: mcp_server.snow_login(instance=INSTANCE)),
            ("elevate", 0, lambda: mcp_server.snow_elevate(instance=INSTANCE)),
            ("search_records_json", [], lambda: mcp_server.snow_record_search("incident", instance=INSTANCE)),
            ("table_fields_json", [], lambda: mcp_server.snow_table_fields("incident", instance=INSTANCE)),
            ("count_records_value", 1, lambda: mcp_server.snow_record_count("incident", instance=INSTANCE)),
            (
                "aggregate_records_json",
                [],
                lambda: mcp_server.snow_record_aggregate("incident", count=True, instance=INSTANCE),
            ),
        ]
        for command_name, return_value, call_tool in cases:
            with self.subTest(command=command_name):
                self.repository.load_config.reset_mock()
                command = Mock(return_value=return_value)
                with patch.object(mcp_server, command_name, command):
                    call_tool()
                self.repository.load_config.assert_called_once_with(instance=INSTANCE)
                self.assertIs(command.call_args[0][0], self.config)

    def test_omitted_instance_lets_repository_resolve_the_default(self):
        with patch.object(mcp_server, "count_records_value", Mock(return_value=1)):
            mcp_server.snow_record_count("incident")
        self.repository.load_config.assert_called_once_with(instance=None)


class ConfigFailureAuditTests(McpToolTestCase):
    def setUp(self):
        super().setUp()
        self.repository.load_config.side_effect = ValueError("bad credentials setup")

    def assert_error_audited(self, tool_name):
        self.log_tool_call.assert_called_once()
        args, kwargs = self.log_tool_call.call_args
        self.assertEqual(args[0], tool_name)
        self.assertEqual(args[2], "error")
        self.assertEqual(kwargs["error"], "bad credentials setup")

    def test_read_only_tools_audit_and_return_error_json(self):
        cases = [
            ("snow_record_search", lambda: mcp_server.snow_record_search("incident")),
            ("snow_table_fields", lambda: mcp_server.snow_table_fields("incident")),
            ("snow_record_count", lambda: mcp_server.snow_record_count("incident")),
            ("snow_record_aggregate", lambda: mcp_server.snow_record_aggregate("incident", count=True)),
        ]
        for tool_name, call_tool in cases:
            with self.subTest(tool=tool_name):
                self.log_tool_call.reset_mock()
                self.assertEqual(json.loads(call_tool()), {"error": "bad credentials setup"})
                self.assert_error_audited(tool_name)

    def test_session_tools_audit_and_reraise(self):
        cases = [
            ("snow_run_script", lambda: mcp_server.snow_run_script("gs.print(1);")),
            ("snow_login", lambda: mcp_server.snow_login()),
            ("snow_elevate", lambda: mcp_server.snow_elevate()),
        ]
        for tool_name, call_tool in cases:
            with self.subTest(tool=tool_name):
                self.log_tool_call.reset_mock()
                with self.assertRaises(ValueError):
                    call_tool()
                self.assert_error_audited(tool_name)


class AuditRedactionTests(unittest.TestCase):
    def test_oauth_values_are_redacted(self):
        redacted = _redact({"client_secret": "s3cret", "access_token": "t0ken", "client_id": "abc"})
        self.assertNotIn("s3cret", redacted.values())
        self.assertNotIn("t0ken", redacted.values())
        self.assertEqual(redacted["client_id"], "abc")
