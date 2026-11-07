"""MCP server mode for ServiceNow CLI"""
import io
import os
import time
import contextlib
from pathlib import Path
from typing import Optional, List

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .commands import login, elevate, run_script, search_records_json, table_fields_json, count_records_value, aggregate_records_json
from .instance_repository import InstanceRepository
from .instance_manager import list_instances
from .audit import log_tool_call
from .config import Config


# -- Annotation presets -------------------------------------------------------

_DESTRUCTIVE = ToolAnnotations(
    destructive_hint=True,
    read_only_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

_READ_ONLY = ToolAnnotations(
    destructive_hint=False,
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

_SENSITIVE_TABLES = frozenset({
    "sys_user",
    "sys_user_role",
    "sys_user_group",
    "sys_user_has_role",
    "sys_properties",
    "sys_auth_profile",
    "cmn_schedule",
    "cmn_location",
    "cmn_department",
})

mcp = FastMCP(
    "ServiceNow CLI",
    instructions=(
        "Tools for executing JavaScript background scripts on a ServiceNow instance, "
        "logging in, elevating privileges, listing configured instances, and querying tables. "
        "Instances are pre-configured via 'snow add'. "
        "Omit 'instance' to use the default configured instance."
    ),
)


def _load_config(instance: Optional[str]) -> Config:
    """Resolve config for a tool call, refusing hosts the user has not configured.

    The instance value comes from the AI client, and load_config applies
    snow_user and snow_pwd to any host, so it must not choose a new one.
    """
    repository = InstanceRepository()
    if instance:
        allowed = set(repository.list_all())
        allowed.add(os.environ.get("snow_instance"))
        if instance not in allowed:
            raise ValueError(f"Instance {instance} is not configured. Run 'snow add {instance}' first.")
    return repository.load_config(instance=instance)


def _resolve_output_path(path: str) -> str:
    """Return the absolute path for an output file, confined to the working directory.

    MCP callers are not trusted with arbitrary writes. They can move the file
    afterwards, so the tools report the absolute location in "saved_to".
    """
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError(f"Absolute output paths are not allowed: {path}")
    if ".." in candidate.parts:
        raise ValueError(f"Path traversal is not allowed in output paths: {path}")
    cwd = Path.cwd().resolve()
    resolved = (cwd / candidate).resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError:
        raise ValueError(f"Output path escapes the working directory: {path}")
    return str(resolved)


def _run_with_capture(fn, *args, **kwargs) -> str:
    """Run a command function and return captured stdout + stderr as a single string."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exit_code = fn(*args, **kwargs)
    output = stdout_buf.getvalue()
    errors = stderr_buf.getvalue()
    parts = []
    if output:
        parts.append(output.rstrip())
    if errors:
        parts.append(f"[stderr]\n{errors.rstrip()}")
    if not parts:
        parts.append("Done." if exit_code == 0 else f"Command failed (exit code {exit_code}).")
    return "\n".join(parts)


@mcp.tool(annotations=_DESTRUCTIVE)
def snow_run_script(script: str, instance: Optional[str] = None) -> str:
    """Execute a JavaScript background script on a ServiceNow instance if snow_record_search, snow_table_fields, snow_record_count are not sufficient.



    Args:
        script: JavaScript source code to run (e.g. "gs.print('hello');")
        instance: ServiceNow instance hostname (e.g. dev1234.service-now.com).
                  Omit to use the default configured instance.
    """
    t0 = time.monotonic()
    try:
        config = _load_config(instance)
        result = _run_with_capture(run_script, config, script_content=script)
        log_tool_call("snow_run_script", {"instance": instance or "(default)", "script_length": len(script)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return result
    except Exception as e:
        log_tool_call("snow_run_script", {"instance": instance or "(default)", "script_length": len(script)}, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise


@mcp.tool(annotations=_DESTRUCTIVE)
def snow_login(instance: Optional[str] = None) -> str:
    """Log in to a ServiceNow instance and persist the session cookie.

    Args:
        instance: ServiceNow instance hostname. Omit to use the default instance.
    """
    t0 = time.monotonic()
    try:
        config = _load_config(instance)
        result = _run_with_capture(login, config)
        log_tool_call("snow_login", {"instance": instance or "(default)"}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return result
    except Exception as e:
        log_tool_call("snow_login", {"instance": instance or "(default)"}, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise


@mcp.tool(annotations=_DESTRUCTIVE)
def snow_elevate(instance: Optional[str] = None) -> str:
    """Elevate privileges to the security_admin role on a ServiceNow instance.

    Args:
        instance: ServiceNow instance hostname. Omit to use the default instance.
    """
    t0 = time.monotonic()
    try:
        config = _load_config(instance)
        result = _run_with_capture(elevate, config)
        log_tool_call("snow_elevate", {"instance": instance or "(default)"}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return result
    except Exception as e:
        log_tool_call("snow_elevate", {"instance": instance or "(default)"}, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise


@mcp.tool(annotations=_READ_ONLY)
def snow_list_instances() -> str:
    """List all configured ServiceNow instances."""
    t0 = time.monotonic()
    result = _run_with_capture(list_instances)
    log_tool_call("snow_list_instances", {}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
    return result


@mcp.tool(annotations=_READ_ONLY)
def snow_record_search(
    table: str,
    query: Optional[str] = None,
    order_by: Optional[List[str]] = None,
    order_by_desc: Optional[List[str]] = None,
    fields: Optional[str] = None,
    limit: Optional[int] = None,
    display_values: str = "both",
    sys_id: bool = False,
    output_file: Optional[str] = None,
    instance: Optional[str] = None,
) -> str:
    """Search records in a ServiceNow table. Returns JSON array of records.

    Prefer this tool over `snow_run_script` for retrieving data.

    Always specify `limit` to avoid large inline responses that consume context.
    Use `output_file` to save results to disk and receive only metadata back —
    ideal for large exports or when the caller does not need the data inline.

    Args:
        table: ServiceNow table name.
        query: Encoded query string (sysparm_query).
        order_by: Field names to sort ascending.
        order_by_desc: Field names to sort descending.
        fields: Comma-separated fields to return.
        limit: Maximum number of records to return. Set this to avoid large responses.
        display_values: One of values, display, both (default both).
        sys_id: Shortcut for fields=sys_id.
        output_file: If set, save JSON results to this path and return only
                     {"saved_to": "...", "count": N, "fields": [...]} instead of all records.
                     Must be relative and stay inside the server's working directory;
                     "saved_to" is the absolute location.
        instance: ServiceNow instance hostname. Omit to use default instance.
    """
    import json as _json
    t0 = time.monotonic()
    audit_params = {"table": table, "query": query, "limit": limit, "instance": instance or "(default)"}
    effective_fields = "sys_id" if sys_id else fields
    warning_prefix = ""
    if table in _SENSITIVE_TABLES:
        warning_prefix = f"WARNING: Accessing sensitive table '{table}'. Handle data with care.\n"

    try:
        safe_path = _resolve_output_path(output_file) if output_file else None
        config = _load_config(instance)
        records = search_records_json(
            config,
            table=table,
            query=query,
            order_by=order_by,
            order_by_desc=order_by_desc,
            fields=effective_fields,
            limit=limit,
            display_values=display_values,
        )
        if output_file:
            with open(safe_path, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(records, ensure_ascii=False, indent=2))
            field_names = list(records[0].keys()) if records else []
            log_tool_call("snow_record_search", {**audit_params, "output_file": output_file, "record_count": len(records)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
            return _json.dumps({"warning": warning_prefix.strip(), "saved_to": safe_path, "count": len(records), "fields": field_names})
        log_tool_call("snow_record_search", {**audit_params, "record_count": len(records)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return warning_prefix + _json.dumps(records, ensure_ascii=False, indent=2)
    except Exception as e:
        log_tool_call("snow_record_search", audit_params, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps({"error": str(e)})


@mcp.tool(annotations=_READ_ONLY)
def snow_table_fields(
    table: str,
    output_file: Optional[str] = None,
    instance: Optional[str] = None,
) -> str:
    """List all fields (including inherited) for a ServiceNow table.

    Returns a JSON array of {field, label, type, references} objects.
    For reference-type fields, 'references' contains the referenced table name.

    For tables with many fields (100+), use output_file to save to disk and
    receive only metadata back — avoiding large inline responses.

    Args:
        table: ServiceNow table name (e.g. incident, cmdb_ci, task).
        output_file: If set, save JSON results to this path and return only
                     {"saved_to": "...", "count": N} instead of all fields.
                     Must be relative and stay inside the server's working directory;
                     "saved_to" is the absolute location.
        instance: ServiceNow instance hostname. Omit to use default instance.
    """
    import json as _json
    t0 = time.monotonic()
    audit_params = {"table": table, "instance": instance or "(default)"}
    warning_prefix = ""
    if table in _SENSITIVE_TABLES:
        warning_prefix = f"WARNING: Accessing sensitive table '{table}'. Handle data with care.\n"

    try:
        safe_path = _resolve_output_path(output_file) if output_file else None
        config = _load_config(instance)
        fields_data = table_fields_json(config, table)
        if output_file:
            with open(safe_path, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(fields_data, ensure_ascii=False, indent=2))
            log_tool_call("snow_table_fields", {**audit_params, "output_file": output_file, "field_count": len(fields_data)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
            return _json.dumps({"warning": warning_prefix.strip(), "saved_to": safe_path, "count": len(fields_data)})
        log_tool_call("snow_table_fields", {**audit_params, "field_count": len(fields_data)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return warning_prefix + _json.dumps(fields_data, ensure_ascii=False, indent=2)
    except Exception as e:
        log_tool_call("snow_table_fields", audit_params, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps({"error": str(e)})


@mcp.tool(annotations=_READ_ONLY)
def snow_record_count(
    table: str,
    query: Optional[str] = None,
    instance: Optional[str] = None,
) -> str:
    """Count records in a ServiceNow table using the Aggregate API.

    Returns {"count": N} JSON. Very lightweight — no context window concern.

    Args:
        table: ServiceNow table name (e.g. incident, change_request).
        query: Encoded query string to filter records (sysparm_query).
        instance: ServiceNow instance hostname. Omit to use default instance.
    """
    import json as _json
    t0 = time.monotonic()
    audit_params = {"table": table, "query": query, "instance": instance or "(default)"}
    try:
        config = _load_config(instance)
        count = count_records_value(config, table, query=query)
        log_tool_call("snow_record_count", {**audit_params, "count": count}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps({"count": count})
    except Exception as e:
        log_tool_call("snow_record_count", audit_params, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps({"error": str(e)})


@mcp.tool(annotations=_READ_ONLY)
def snow_record_aggregate(
    table: str,
    query: Optional[str] = None,
    group_by: Optional[List[str]] = None,
    count: bool = False,
    avg: Optional[List[str]] = None,
    sum_fields: Optional[List[str]] = None,
    min_fields: Optional[List[str]] = None,
    max_fields: Optional[List[str]] = None,
    having: Optional[str] = None,
    display_values: str = "both",
    output_file: Optional[str] = None,
    instance: Optional[str] = None,
) -> str:
    """Aggregate records in a ServiceNow table using the Aggregate API.

    Returns a JSON array of result rows. At least one aggregate function must be specified.

    Args:
        table: ServiceNow table name (e.g. incident, change_request).
        query: Encoded query string to filter records (sysparm_query).
        group_by: Field names to group by.
        count: If true, include COUNT in results.
        avg: Field names to compute AVG for.
        sum_fields: Field names to compute SUM for.
        min_fields: Field names to compute MIN for.
        max_fields: Field names to compute MAX for.
        having: HAVING clause filter (e.g. COUNT>10).
        display_values: One of values, display, both (default both).
        output_file: If set, save JSON results to this file and return only metadata.
                     Must be relative and stay inside the server's working directory;
                     "saved_to" is the absolute location.
        instance: ServiceNow instance hostname. Omit to use default instance.
    """
    import json as _json
    t0 = time.monotonic()
    audit_params = {"table": table, "query": query, "group_by": group_by, "instance": instance or "(default)"}
    try:
        safe_path = _resolve_output_path(output_file) if output_file else None
        config = _load_config(instance)
        records = aggregate_records_json(
            config,
            table=table,
            query=query,
            group_by=group_by,
            count=count,
            avg=avg,
            sum_fields=sum_fields,
            min_fields=min_fields,
            max_fields=max_fields,
            having=having,
            display_values=display_values,
        )
        if output_file:
            with open(safe_path, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(records, ensure_ascii=False, indent=2))
            log_tool_call("snow_record_aggregate", {**audit_params, "output_file": output_file, "record_count": len(records)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
            return _json.dumps({"saved_to": safe_path, "count": len(records)})
        log_tool_call("snow_record_aggregate", {**audit_params, "record_count": len(records)}, "success", duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps(records, ensure_ascii=False, indent=2)
    except Exception as e:
        log_tool_call("snow_record_aggregate", audit_params, "error", error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        return _json.dumps({"error": str(e)})


def serve():
    """Start the MCP server using stdio transport."""
    mcp.run()
