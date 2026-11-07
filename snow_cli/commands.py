"""ServiceNow command implementations"""
import contextlib
import csv
import html
import io
import json
import os
import re
import secrets
import sys
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import Config
from .client import ScriptTokenError, ServiceNowClient
from .timeouts import SCRIPT_TIMEOUT


DISPLAY_VALUE_MAP = {
    "values": "false",
    "display": "true",
    "both": "all",
}

SCRIPT_OUTPUT_PREFIX = "*** Script:"
PRE_BLOCK_RE = re.compile(r"<PRE\b[^>]*>(.*?)</PRE>", re.DOTALL | re.IGNORECASE)
BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


class OutputFormat(str, Enum):
    TABLE = "table"
    TSV   = "tsv"
    CSV   = "csv"
    JSON  = "json"
    XML   = "xml"
    EXCEL = "excel"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def aggregate_choices(cls) -> "List[OutputFormat]":
        return [f for f in cls if f not in {cls.XML, cls.EXCEL}]


def _ensure_ui_session(client: ServiceNowClient) -> None:
    if not client.supports_ui_session:
        raise ValueError(
            "Background scripts and elevation need a UI session, which OAuth sign-in does not create. "
            "Configure this instance with a username and password to use this command."
        )


def login(config: Config) -> int:
    """Login to ServiceNow instance"""
    try:
        config.ensure_credentials_set()

        client = ServiceNowClient(config)
        if not client.supports_ui_session:
            client.authenticate()
            print(f"Successfully signed in to {config.instance}")
            return 0

        login_token = client.get_login_token()

        response = client.scraping_post(
            "/login.do",
            data={
                "sysparm_ck": login_token,
                "user_name": config.user,
                "user_password": config.password,
                "ni.nolog.user_password": "true",
                "ni.noecho.user_name": "true",
                "ni.noecho.user_password": "true",
                "screensize": "1920x1080",
                "sys_action": "sysverb_login",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        if response.status_code == 200:
            print(f"Successfully logged in to {config.instance}")
            return 0
        else:
            print(f"Login failed with status code: {response.status_code}", file=sys.stderr)
            return 1

    except Exception as e:
        print(f"Login error: {e}", file=sys.stderr)
        return 1


def elevate(config: Config) -> int:
    """Elevate user privileges (e.g., security_admin role)"""
    try:
        config.ensure_instance_set()

        client = ServiceNowClient(config)
        _ensure_ui_session(client)
        token = client.get_elevate_token()

        response = client.scraping_post(
            "/api/now/ui/impersonate/role",
            headers={
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-US,en;q=0.9",
                "X-WantSessionNotificationMessages": "true",
                "X-UserToken": token,
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Connection": "keep-alive",
            },
            json={"roles": "security_admin"},
        )

        if response.status_code in (200, 201):
            print(f"Successfully elevated privileges on {config.instance}")
            return 0
        else:
            print(f"Elevation failed with status code: {response.status_code}", file=sys.stderr)
            print(f"Response: {response.text}", file=sys.stderr)
            return 1

    except Exception as e:
        print(f"Elevation error: {e}", file=sys.stderr)
        return 1


def run_script(
    config: Config,
    script_file: Optional[str] = None,
    script_content: Optional[str] = None,
    auto_login: bool = False,
) -> int:
    """Run a background script on ServiceNow"""
    try:
        config.ensure_instance_set()

        client = ServiceNowClient(config)
        _ensure_ui_session(client)

        if script_content is None:
            if script_file and script_file != "-":
                with open(script_file, "r") as f:
                    script_content = f.read()
            else:
                script_content = sys.stdin.read()

        try:
            return _run_script_once(client, config, script_content)
        except ScriptTokenError as exc:
            if not auto_login:
                raise

            print(str(exc), file=sys.stderr)

            if _run_command_with_output_on_stderr(login, config) != 0:
                return 1
            if _run_command_with_output_on_stderr(elevate, config) != 0:
                return 1

            client.reload_cookies()
            return _run_script_once(client, config, script_content)

    except FileNotFoundError:
        print(f"Script file not found: {script_file}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Script execution error: {e}", file=sys.stderr)
        return 1


def _run_command_with_output_on_stderr(fn, *args, **kwargs) -> int:
    with contextlib.redirect_stdout(sys.stderr), contextlib.redirect_stderr(sys.stderr):
        return fn(*args, **kwargs)


def _run_script_once(client: ServiceNowClient, config: Config, script_content: str) -> int:
    token = client.get_script_token()
    start_marker, end_marker = _generate_output_markers()
    wrapped_script = _wrap_script_with_output_markers(script_content, start_marker, end_marker)

    response = client.scraping_post(
        "/sys.scripts.do",
        headers={
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        },
        data={
            "sysparm_ck": token,
            "runscript": "Run script",
            "record_for_rollback": "on",
            "quota_managed_transaction": "on",
            "script": wrapped_script,
        },
        timeout=SCRIPT_TIMEOUT,
    )

    if response.status_code != 200:
        print(f"Script execution failed with status: {response.status_code}", file=sys.stderr)
        return 1

    # Save raw output for debugging
    output_file = config.tmp_dir / "last_run_output.txt"
    output_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(output_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(response.text)

    # Parse and display output
    _parse_and_display_output(response.text, start_marker=start_marker, end_marker=end_marker)
    return 0


def _generate_output_markers() -> Tuple[str, str]:
    token = secrets.token_hex(16)
    return (
        f"__SNOW_RUN_START_{token}__",
        f"__SNOW_RUN_END_{token}__",
    )


def _wrap_script_with_output_markers(script_content: str, start_marker: str, end_marker: str) -> str:
    # Keep the wrapper to simple surrounding prints so the user's script stays
    # as close as possible to its original execution shape.
    return "\n".join([
        f"gs.print({json.dumps(start_marker)});",
        script_content,
        f"gs.print({json.dumps(end_marker)});",
    ])


def _extract_output_events(html_response: str) -> List[Tuple[str, str]]:
    events: List[Tuple[str, str]] = []

    for block in PRE_BLOCK_RE.findall(html_response):
        normalized_block = BR_TAG_RE.sub("\n", html.unescape(block))
        for raw_line in normalized_block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(SCRIPT_OUTPUT_PREFIX):
                events.append(("stdout", line[len(SCRIPT_OUTPUT_PREFIX):].lstrip()))
            else:
                events.append(("stderr", line))

    return events


def _find_marker_indexes(stdout_lines: Sequence[str], start_marker: Optional[str], end_marker: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not start_marker or not end_marker:
        return None, None

    start_index = None
    for index, line in enumerate(stdout_lines):
        if line == start_marker:
            start_index = index
            break

    if start_index is None:
        return None, None

    end_index = None
    for index in range(start_index + 1, len(stdout_lines)):
        if stdout_lines[index] == end_marker:
            end_index = index
            break

    return start_index, end_index


class _ParseState(Enum):
    PASSTHROUGH  = auto()
    BEFORE_START = auto()
    IN_SCRIPT    = auto()
    AFTER_END    = auto()


def _parse_output_lines(
    html_response: str,
    start_marker: Optional[str] = None,
    end_marker: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    events = _extract_output_events(html_response)
    stdout_lines = [text for channel, text in events if channel == "stdout"]
    start_index, end_index = _find_marker_indexes(stdout_lines, start_marker, end_marker)

    state = _ParseState.PASSTHROUGH if start_index is None else _ParseState.BEFORE_START
    stdout_position = -1
    filtered_stdout: List[str] = []
    filtered_stderr: List[str] = []

    for channel, text in events:
        if channel != "stdout":
            filtered_stderr.append(text)
            continue

        stdout_position += 1

        if state == _ParseState.PASSTHROUGH:
            filtered_stdout.append(text)

        elif state == _ParseState.BEFORE_START:
            if stdout_position == start_index:
                state = _ParseState.IN_SCRIPT if end_index is not None else _ParseState.PASSTHROUGH
            else:
                filtered_stderr.append(text)

        elif state == _ParseState.IN_SCRIPT:
            if stdout_position == end_index:
                state = _ParseState.AFTER_END
            else:
                filtered_stdout.append(text)

        elif state == _ParseState.AFTER_END:
            filtered_stderr.append(text)

    if start_marker and end_marker:
        if start_index is None and end_marker in stdout_lines:
            filtered_stderr.append(
                "Warning: ServiceNow output start marker was not found; showing best-effort stdout."
            )
        elif start_index is not None and end_index is None:
            filtered_stderr.append(
                "Warning: ServiceNow output end marker was not found; showing best-effort stdout."
            )

    return filtered_stdout, filtered_stderr


def _parse_and_display_output(
    html_response: str,
    start_marker: Optional[str] = None,
    end_marker: Optional[str] = None,
):
    """Parse ServiceNow script output and display stdout/stderr separately"""
    stdout_parts, stderr_parts = _parse_output_lines(
        html_response,
        start_marker=start_marker,
        end_marker=end_marker,
    )

    for line in stdout_parts:
        print(line)

    for line in stderr_parts:
        if line:
            print(line, file=sys.stderr)


_DEFAULT_PAGE_SIZE = 1000

# Matches rel="next" in a Link header
_RE_LINK_NEXT = re.compile(r'rel=["\']next["\']')


def _has_next_page(response) -> bool:
    """Return True if the response Link header contains rel="next"."""
    link_header = response.headers.get("Link", "")
    return bool(_RE_LINK_NEXT.search(link_header))


def _fetch_records(
    client: "ServiceNowClient",
    table: str,
    query: Optional[str] = None,
    order_by: Optional[list] = None,
    order_by_desc: Optional[list] = None,
    fields: Optional[str] = None,
    limit: Optional[int] = None,
    display_values: str = "both",
) -> list:
    """Fetch records from Table API and return as a list of dicts. Raises on error.

    Automatically paginates using sysparm_offset so that all matching records are
    returned regardless of the server-side row cap. Uses the Link response header
    (rel=next) as the authoritative signal for more pages. This handles cases where
    ACL post-filtering causes fewer records than requested to be returned per page.
    Falls back to the page-size heuristic when the header is absent.

    When *limit* is set, at most that many records are returned.
    """
    if limit is not None and limit <= 0:
        return []

    query_parts = []
    if query:
        query_parts.append(query)
    if order_by:
        query_parts.extend([f"ORDERBY{field}" for field in order_by if field])
    if order_by_desc:
        query_parts.extend([f"ORDERBYDESC{field}" for field in order_by_desc if field])

    base_params = {
        "sysparm_display_value": DISPLAY_VALUE_MAP[display_values],
        "sysparm_query": "^".join(query_parts),
    }
    if fields:
        base_params["sysparm_fields"] = fields

    all_records: list = []
    offset = 0

    while True:
        remaining = limit - len(all_records) if limit is not None else None
        page_size = min(_DEFAULT_PAGE_SIZE, remaining) if remaining is not None else _DEFAULT_PAGE_SIZE

        params = {**base_params, "sysparm_limit": str(page_size), "sysparm_offset": str(offset)}
        response = client.rest_get(f"/api/now/table/{table}", params=params)

        if response.status_code != 200:
            raise RuntimeError(
                f"Record query failed with status code: {response.status_code}\n{response.text}"
            )

        payload = response.json()
        page = payload.get("result", [])
        if isinstance(page, dict):
            page = [page]

        all_records.extend(page)

        if limit is not None and len(all_records) >= limit:
            break

        if _has_next_page(response):
            offset += page_size
        elif len(page) < page_size:
            break
        else:
            offset += page_size

    return all_records


def _get_table_hierarchy(client: "ServiceNowClient", table_name: str) -> list:
    """Return list of table names from table_name up to the root (most-specific first)."""
    hierarchy = []
    current = table_name
    visited = set()
    while current and current not in visited:
        visited.add(current)
        hierarchy.append(current)
        resp = client.rest_get(
            "/api/now/table/sys_db_object",
            params={
                "sysparm_query": f"name={current}",
                "sysparm_fields": "super_class.name",
                "sysparm_limit": "1",
                "sysparm_display_value": "false",
            },
        )
        if resp.status_code != 200:
            break
        rows = resp.json().get("result") or []
        if not rows:
            break
        parent = rows[0].get("super_class.name") or ""
        if isinstance(parent, dict):
            parent = parent.get("value") or parent.get("display_value") or ""
        current = parent.strip() if parent else ""
    return hierarchy


def _fetch_table_fields(client: "ServiceNowClient", table_name: str) -> list:
    """Return all fields (including inherited) for a table. Raises on error.

    Each entry: {field, label, type, references, defined_on}
    """
    hierarchy = _get_table_hierarchy(client, table_name)
    if not hierarchy:
        raise RuntimeError(f"Table '{table_name}' not found or not accessible.")

    table_in_query = ",".join(hierarchy)
    resp = client.rest_get(
        "/api/now/table/sys_dictionary",
        params={
            "sysparm_query": f"nameIN{table_in_query}^elementISNOTEMPTY",
            "sysparm_fields": "element,column_label,internal_type,reference,name",
            "sysparm_limit": "10000",
            "sysparm_display_value": "all",
            "sysparm_no_count": "true",
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch fields (HTTP {resp.status_code}): {resp.text[:200]}"
        )

    raw = resp.json().get("result") or []

    def _val(cell):
        if isinstance(cell, dict):
            return cell.get("value") or ""
        return str(cell) if cell else ""

    # Build priority map: lower index in hierarchy = higher priority (child wins)
    table_priority = {t: i for i, t in enumerate(hierarchy)}

    # Collect all field entries
    entries = []
    for row in raw:
        field_name = _val(row.get("element"))
        if not field_name:
            continue
        entries.append({
            "_table": _val(row.get("name")),
            "field": field_name,
            "label": _val(row.get("column_label")),
            "type": _val(row.get("internal_type")),
            "references": _val(row.get("reference")),
        })

    # Deduplicate: child wins for effective field data; ancestor wins for defined_on
    seen = {}  # field → {"child_prio", "child_entry", "anc_prio", "origin"}
    for entry in entries:
        f = entry["field"]
        prio = table_priority.get(entry["_table"], len(hierarchy))
        if f not in seen:
            seen[f] = {"child_prio": prio, "child_entry": entry,
                       "anc_prio": prio, "origin": entry["_table"]}
        else:
            rec = seen[f]
            if prio < rec["child_prio"]:
                rec["child_prio"] = prio
                rec["child_entry"] = entry
            if prio > rec["anc_prio"]:
                rec["anc_prio"] = prio
                rec["origin"] = entry["_table"]

    result = sorted(
        (
            {**{k: v for k, v in rec["child_entry"].items() if k != "_table"},
             "defined_on": rec["origin"]}
            for rec in seen.values()
        ),
        key=lambda x: x["field"],
    )
    return result


def table_fields(
    config: Config,
    table_name: str,
    fmt: str = "table",
    output_file: Optional[str] = None,
) -> int:
    """Output all fields for a ServiceNow table (CLI output)."""
    try:
        config.ensure_credentials_set()
        if fmt not in FORMAT_CHOICES:
            print(f"Invalid format. Use one of: {', '.join(FORMAT_CHOICES)}", file=sys.stderr)
            return 1
        if fmt == OutputFormat.EXCEL and not output_file:
            print("--output FILE is required when --format is excel.", file=sys.stderr)
            return 1

        client = ServiceNowClient(config)
        fields_data = _fetch_table_fields(client, table_name)
        if not fields_data:
            print(f"No fields found for table '{table_name}'.")
            return 0

        columns = ["field", "label", "type", "references", "defined_on"]
        _output_records(fields_data, columns, no_header=False, display_values="values",
                        fmt=fmt, output_file=output_file, table="sys_dictionary")
        return 0
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Table fields error: {e}", file=sys.stderr)
        return 1


def table_fields_json(config: Config, table_name: str) -> list:
    """Return all fields for a table as a list of dicts. Raises on error."""
    config.ensure_credentials_set()
    client = ServiceNowClient(config)
    return _fetch_table_fields(client, table_name)


def _fetch_record_count(client: "ServiceNowClient", table: str, query: Optional[str] = None) -> int:
    """Call the Aggregate API and return the record count. Raises on error."""
    params = {"sysparm_count": "true"}
    if query:
        params["sysparm_query"] = query
    response = client.rest_get(f"/api/now/stats/{table}", params=params)
    if response.status_code != 200:
        raise RuntimeError(
            f"Record count failed with status code: {response.status_code}\n{response.text}"
        )
    return int(response.json()["result"]["stats"]["count"])


def count_records(config: Config, table: str, query: Optional[str] = None) -> int:
    """Print the count of matching records (CLI output). Returns exit code."""
    try:
        config.ensure_credentials_set()
        client = ServiceNowClient(config)
        count = _fetch_record_count(client, table, query)
        print(count)
        return 0
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Record count error: {e}", file=sys.stderr)
        return 1


def count_records_value(config: Config, table: str, query: Optional[str] = None) -> int:
    """Return the count as an integer. Raises on error."""
    config.ensure_credentials_set()
    client = ServiceNowClient(config)
    return _fetch_record_count(client, table, query)


def _fetch_aggregate_records(
    client: "ServiceNowClient",
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
) -> List[dict]:
    """Call the Aggregate API and return flattened result rows. Raises on error."""
    params: dict = {
        "sysparm_display_value": DISPLAY_VALUE_MAP[display_values],
    }
    if query:
        params["sysparm_query"] = query
    if group_by:
        params["sysparm_group_by"] = ",".join(group_by)
    if count:
        params["sysparm_count"] = "true"
    if avg:
        params["sysparm_avg"] = ",".join(avg)
    if sum_fields:
        params["sysparm_sum"] = ",".join(sum_fields)
    if min_fields:
        params["sysparm_min"] = ",".join(min_fields)
    if max_fields:
        params["sysparm_max"] = ",".join(max_fields)
    if having:
        params["sysparm_having"] = having

    response = client.rest_get(f"/api/now/stats/{table}", params=params)
    if response.status_code != 200:
        raise RuntimeError(
            f"Aggregate query failed with status code: {response.status_code}\n{response.text}"
        )

    payload = response.json()
    raw_results = payload.get("result", [])
    if isinstance(raw_results, dict):
        # No group-by: single stats object returned directly
        raw_results = [raw_results]

    rows = []
    for item in raw_results:
        row: dict = {}
        # Flatten groupby_fields list into the row
        for gf in item.get("groupby_fields", []):
            field_name = gf.get("field", "")
            display_val = gf.get("display_value", "")
            value_val = gf.get("value", "")
            if display_values == "display":
                row[field_name] = display_val
            elif display_values == "values":
                row[field_name] = value_val
            else:
                row[field_name] = (
                    f"{display_val} ({value_val})" if display_val != value_val else display_val
                )
        # Flatten stats
        for stat_name, stat_val in item.get("stats", {}).items():
            row[stat_name] = stat_val
        rows.append(row)
    return rows


def aggregate_records(
    config: Config,
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
    fmt: str = "table",
    output_file: Optional[str] = None,
) -> int:
    """Run an aggregate query and print results (CLI output). Returns exit code."""
    try:
        config.ensure_credentials_set()

        if not count and not avg and not sum_fields and not min_fields and not max_fields:
            print(
                "At least one aggregate function must be specified: "
                "--count, --avg, --sum, --min, or --max.",
                file=sys.stderr,
            )
            return 1

        if display_values not in DISPLAY_VALUE_MAP:
            print(
                "Invalid display values mode. Use one of: values, display, both.",
                file=sys.stderr,
            )
            return 1

        if fmt not in OutputFormat.aggregate_choices():
            print(
                f"Invalid format. Use one of: {', '.join(str(f) for f in OutputFormat.aggregate_choices())}",
                file=sys.stderr,
            )
            return 1

        client = ServiceNowClient(config)
        records = _fetch_aggregate_records(
            client,
            table,
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

        if not records:
            if fmt == OutputFormat.JSON:
                _write_or_print("[]", output_file)
            else:
                print("No results.")
            return 0

        columns = list(records[0].keys())
        _output_records(records, columns, no_header=False, display_values="values",
                        fmt=fmt, output_file=output_file, table=table)
        return 0

    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Aggregate query error: {e}", file=sys.stderr)
        return 1


def aggregate_records_json(
    config: Config,
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
) -> List[dict]:
    """Run an aggregate query and return results as a list of dicts. Raises on error."""
    config.ensure_credentials_set()
    if display_values not in DISPLAY_VALUE_MAP:
        raise ValueError(f"Invalid display_values '{display_values}'. Use one of: values, display, both.")
    client = ServiceNowClient(config)
    return _fetch_aggregate_records(
        client,
        table,
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


def search_records(
    config: Config,
    table: str,
    query: Optional[str] = None,
    order_by: Optional[list] = None,
    order_by_desc: Optional[list] = None,
    fields: Optional[str] = None,
    limit: Optional[int] = None,
    no_header: bool = False,
    sys_id: bool = False,
    display_values: str = "both",
    fmt: str = "table",
    output_file: Optional[str] = None,
) -> int:
    """Search records in a ServiceNow table using Table API (CLI output)."""
    try:
        config.ensure_credentials_set()

        if sys_id:
            fields = "sys_id"
            no_header = True

        if display_values not in DISPLAY_VALUE_MAP:
            print(
                "Invalid display values mode. Use one of: values, display, both.",
                file=sys.stderr,
            )
            return 1

        if fmt not in FORMAT_CHOICES:
            print(f"Invalid format. Use one of: {', '.join(FORMAT_CHOICES)}", file=sys.stderr)
            return 1

        if fmt == OutputFormat.EXCEL and not output_file:
            print("--output FILE is required when --format is excel.", file=sys.stderr)
            return 1

        client = ServiceNowClient(config)
        records = _fetch_records(client, table, query, order_by, order_by_desc, fields, limit, display_values)

        if not records:
            if fmt not in {OutputFormat.JSON, OutputFormat.XML}:
                print("No records found.")
            elif fmt == OutputFormat.JSON:
                _write_or_print("[]", output_file)
            elif fmt == OutputFormat.XML:
                _write_or_print(_build_xml([], table, display_values), output_file)
            return 0

        selected_fields = _resolve_selected_fields(fields, records[0])
        _output_records(records, selected_fields, no_header, display_values, fmt, output_file, table)
        return 0
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("Failed to parse API response as JSON.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Record query error: {e}", file=sys.stderr)
        return 1


def search_records_json(
    config: Config,
    table: str,
    query: Optional[str] = None,
    order_by: Optional[list] = None,
    order_by_desc: Optional[list] = None,
    fields: Optional[str] = None,
    limit: Optional[int] = None,
    display_values: str = "both",
) -> list:
    """Search records and return as a list of dicts (for programmatic/MCP use). Raises on error."""
    config.ensure_credentials_set()
    if display_values not in DISPLAY_VALUE_MAP:
        raise ValueError(f"Invalid display_values '{display_values}'. Use one of: values, display, both.")
    client = ServiceNowClient(config)
    return _fetch_records(client, table, query, order_by, order_by_desc, fields, limit, display_values)


def _resolve_selected_fields(fields: Optional[str], sample_record: dict) -> list:
    if fields:
        return [field.strip() for field in fields.split(",") if field.strip()]
    return list(sample_record.keys())


def _format_field_value(value, display_values: str) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if display_values == "both":
            display = value.get("display_value", "")
            raw = value.get("value", "")
            if display == raw:
                return str(display)
            return f"{display} ({raw})"
        if display_values == "display":
            return str(value.get("display_value", ""))
        return str(value.get("value", ""))
    return str(value)


def _validate_output_file(path: str) -> str:
    """Validate output file path.
    
    Resolves the path to an absolute path.
    """
    return str(Path(path).resolve())


def _write_or_print(text: str, output_file: Optional[str]):
    if output_file:
        safe_path = _validate_output_file(output_file)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Written to {output_file}")
    else:
        print(text)


def _output_records(
    records: list,
    fields: list,
    no_header: bool,
    display_values: str,
    fmt: str,
    output_file: Optional[str],
    table: str,
):
    _FORMATTERS[fmt](records, fields, no_header, display_values, output_file, table)


def _output_table(records: list, fields: list, no_header: bool, display_values: str, output_file: Optional[str]):
    from tabulate import tabulate
    rows = [
        [_format_field_value(r.get(f), display_values) for f in fields]
        for r in records
    ]
    headers = [] if no_header else fields
    text = tabulate(rows, headers=headers, tablefmt="simple")
    _write_or_print(text, output_file)


def _output_tsv(records: list, fields: list, no_header: bool, display_values: str, output_file: Optional[str]):
    lines = []
    if not no_header:
        lines.append("\t".join(fields))
    for record in records:
        lines.append("\t".join(_format_field_value(record.get(f), display_values) for f in fields))
    _write_or_print("\n".join(lines), output_file)


def _output_csv(records: list, fields: list, no_header: bool, display_values: str, output_file: Optional[str]):
    buf = io.StringIO()
    writer = csv.writer(buf)
    if not no_header:
        writer.writerow(fields)
    for record in records:
        writer.writerow([_format_field_value(record.get(f), display_values) for f in fields])
    _write_or_print(buf.getvalue().rstrip("\r\n"), output_file)


def _build_xml(records: list, table: str, display_values: str) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    safe_table = re.sub(r"[^a-zA-Z0-9_]", "_", table)
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>', f'<unload unload_date="{now}">']
    for record in records:
        parts.append(f'<{safe_table} action="INSERT_OR_UPDATE">')
        for field_name, value in record.items():
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", field_name)
            if isinstance(value, dict):
                raw = str(value.get("value", "") or "")
                display = str(value.get("display_value", "") or "")
                if display_values == "display":
                    parts.append(f"  <{safe_name}>{html.escape(display)}</{safe_name}>")
                elif display_values == "values":
                    parts.append(f"  <{safe_name}>{html.escape(raw)}</{safe_name}>")
                else:  # both
                    if display and display != raw:
                        parts.append(f'  <{safe_name} display_value="{html.escape(display)}">{html.escape(raw)}</{safe_name}>')
                    else:
                        parts.append(f"  <{safe_name}>{html.escape(raw)}</{safe_name}>")
            else:
                parts.append(f"  <{safe_name}>{html.escape(str(value or ''))}</{safe_name}>")
        parts.append(f"</{safe_table}>")
    parts.append("</unload>")
    return "\n".join(parts)


def _output_excel(records: list, fields: list, no_header: bool, display_values: str, output_file: str):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    row_offset = 0
    if not no_header:
        ws.append(fields)
        row_offset = 1
    for record in records:
        ws.append([_format_field_value(record.get(f), display_values) for f in fields])
    wb.save(_validate_output_file(output_file))
    print(f"Written to {output_file}")


_FORMATTERS = {
    "table": lambda r, f, nh, dv, of, t: _output_table(r, f, nh, dv, of),
    "tsv":   lambda r, f, nh, dv, of, t: _output_tsv(r, f, nh, dv, of),
    "csv":   lambda r, f, nh, dv, of, t: _output_csv(r, f, nh, dv, of),
    "json":  lambda r, f, nh, dv, of, t: _write_or_print(
                 json.dumps(r, ensure_ascii=False, indent=2), of),
    "xml":   lambda r, f, nh, dv, of, t: _write_or_print(_build_xml(r, t, dv), of),
    "excel": lambda r, f, nh, dv, of, t: _output_excel(r, f, nh, dv, of),
}
# OutputFormat is authoritative — keep _FORMATTERS keys in sync with its members.
FORMAT_CHOICES = list(OutputFormat)
