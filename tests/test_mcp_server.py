import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
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
        self.repository.list_all.return_value = {INSTANCE: {"user": "admin"}}
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


class InstanceAllowlistTests(McpToolTestCase):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "snow_instance"}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unconfigured_instance_is_refused_before_loading_credentials(self):
        command = Mock(return_value=1)
        with patch.object(mcp_server, "count_records_value", command):
            result = json.loads(mcp_server.snow_record_count("incident", instance="evil.example.com"))
        self.assertIn("not configured", result["error"])
        self.repository.load_config.assert_not_called()
        command.assert_not_called()
        self.assertEqual(self.log_tool_call.call_args[0][2], "error")

    def test_every_tool_refuses_unconfigured_instance(self):
        host = "evil.example.com"
        for call_tool in (
            lambda: mcp_server.snow_run_script("gs.print(1);", instance=host),
            lambda: mcp_server.snow_login(instance=host),
            lambda: mcp_server.snow_elevate(instance=host),
        ):
            with self.assertRaisesRegex(ValueError, "not configured"):
                call_tool()
        for call_tool in (
            lambda: mcp_server.snow_record_search("incident", instance=host),
            lambda: mcp_server.snow_table_fields("incident", instance=host),
            lambda: mcp_server.snow_record_aggregate("incident", count=True, instance=host),
        ):
            self.assertIn("not configured", json.loads(call_tool())["error"])
        self.repository.load_config.assert_not_called()

    def test_instance_from_snow_instance_env_is_allowed(self):
        with patch.dict(os.environ, {"snow_instance": "env.example.com"}), \
             patch.object(mcp_server, "count_records_value", Mock(return_value=1)):
            mcp_server.snow_record_count("incident", instance="env.example.com")
        self.repository.load_config.assert_called_once_with(instance="env.example.com")


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


class OutputFileSandboxTests(McpToolTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        self.commands = {}
        for name, value in (
            ("search_records_json", [{"sys_id": "1"}]),
            ("table_fields_json", [{"field": "a"}]),
            ("aggregate_records_json", [{"count": 1}]),
        ):
            command = Mock(return_value=value)
            self.commands[name] = command
            patcher = patch.object(mcp_server, name, command)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tools = [
            ("search_records_json", lambda f: mcp_server.snow_record_search("incident", output_file=f)),
            ("table_fields_json", lambda f: mcp_server.snow_table_fields("incident", output_file=f)),
            ("aggregate_records_json", lambda f: mcp_server.snow_record_aggregate("incident", count=True, output_file=f)),
        ]

    def test_relative_path_is_written_and_absolute_location_returned(self):
        for command_name, call_tool in self.tools:
            with self.subTest(command=command_name):
                filename = f"{command_name}.json"
                result = json.loads(call_tool(filename))
                self.assertEqual(result["saved_to"], str(self.cwd / filename))
                self.assertTrue((self.cwd / filename).exists())

    def test_paths_outside_working_directory_are_rejected_before_querying(self):
        outside = self.root / "outside.json"
        (self.cwd / "link").symlink_to(self.root)
        for path in (str(outside), "../outside.json", "sub/../../outside.json", "link/outside.json"):
            for command_name, call_tool in self.tools:
                with self.subTest(command=command_name, path=path):
                    self.commands[command_name].reset_mock()
                    self.assertIn("error", json.loads(call_tool(path)))
                    self.commands[command_name].assert_not_called()
        self.assertFalse(outside.exists())


class AuditOutcomeTests(McpToolTestCase):
    def tools(self):
        return [
            ("snow_run_script", "run_script", lambda: mcp_server.snow_run_script("gs.print(1);")),
            ("snow_login", "login", lambda: mcp_server.snow_login()),
            ("snow_elevate", "elevate", lambda: mcp_server.snow_elevate()),
            ("snow_list_instances", "list_instances", lambda: mcp_server.snow_list_instances()),
        ]

    def run_tool(self, command_name, exit_code, call_tool, output="done"):
        def command(*args, **kwargs):
            print(output)
            return exit_code

        self.log_tool_call.reset_mock()
        with patch.object(mcp_server, command_name, command):
            return call_tool()

    def test_failed_command_is_audited_as_error_and_still_returns_output(self):
        for tool_name, command_name, call_tool in self.tools():
            with self.subTest(tool=tool_name):
                result = self.run_tool(command_name, 1, call_tool, output="it failed")
                self.assertEqual(result, "it failed")
                args, kwargs = self.log_tool_call.call_args
                self.assertEqual(args[0], tool_name)
                self.assertEqual(args[2], "error")
                self.assertEqual(kwargs["error"], "exit code 1")

    def test_successful_command_is_audited_as_success(self):
        for tool_name, command_name, call_tool in self.tools():
            with self.subTest(tool=tool_name):
                self.run_tool(command_name, 0, call_tool)
                args, kwargs = self.log_tool_call.call_args
                self.assertEqual(args[2], "success")
                self.assertNotIn("error", kwargs)


class AuditInstanceTests(McpToolTestCase):
    def test_resolved_host_is_audited_when_instance_is_omitted(self):
        cases = [
            ("run_script", 0, lambda: mcp_server.snow_run_script("gs.print(1);")),
            ("login", 0, lambda: mcp_server.snow_login()),
            ("elevate", 0, lambda: mcp_server.snow_elevate()),
            ("search_records_json", [], lambda: mcp_server.snow_record_search("incident")),
            ("table_fields_json", [], lambda: mcp_server.snow_table_fields("incident")),
            ("count_records_value", 1, lambda: mcp_server.snow_record_count("incident")),
            ("aggregate_records_json", [], lambda: mcp_server.snow_record_aggregate("incident", count=True)),
        ]
        for command_name, return_value, call_tool in cases:
            with self.subTest(command=command_name):
                self.log_tool_call.reset_mock()
                with patch.object(mcp_server, command_name, Mock(return_value=return_value)):
                    call_tool()
                self.assertEqual(self.log_tool_call.call_args[0][1]["instance"], INSTANCE)

    def test_failure_after_config_load_audits_the_resolved_host(self):
        with patch.object(mcp_server, "count_records_value", Mock(side_effect=RuntimeError("boom"))):
            mcp_server.snow_record_count("incident")
        self.assertEqual(self.log_tool_call.call_args[0][1]["instance"], INSTANCE)

    def test_config_failure_audits_the_requested_value(self):
        self.repository.load_config.side_effect = ValueError("bad")
        mcp_server.snow_record_count("incident")
        self.assertEqual(self.log_tool_call.call_args[0][1]["instance"], "(default)")
