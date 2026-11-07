# snow-cli

![Python](https://img.shields.io/badge/python-3.8%2B-blue) ![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)

A cross-platform CLI and MCP server for ServiceNow — run background scripts, query tables, inspect schemas, and connect AI assistants to your instances.

---

## Connect AI Assistants to ServiceNow

Add `snow mcp` to Claude Desktop, GitHub Copilot, or any MCP-compatible AI assistant, and your AI gets native access to your ServiceNow instance.

```json
{
  "mcpServers": {
    "servicenow": {
      "command": "snow",
      "args": ["mcp"]
    }
  }
}
```

Once connected, you can ask your AI assistant things like:

- *"How many P1 incidents are open right now?"* → calls `snow_record_count`
- *"Show me all incidents assigned to the network team grouped by priority"* → calls `snow_record_aggregate`
- *"What fields does the `cmdb_ci_server` table have?"* → calls `snow_table_fields`
- *"Run this script on my dev instance"* → calls `snow_run_script`

All MCP calls are logged to `~/.snow-run/audit.log`. See [SECURITY.md](SECURITY.md) for the full threat model.

---

## Terminal Scripting & Querying

Run background scripts from file or stdin, query any table, and inspect schemas — from your terminal on macOS, Linux, and Windows.

**Run a background script:**

Scripts execute server-side on your ServiceNow instance — the full ServiceNow API is available exactly as it is in the platform's Script editor: `GlideRecord`, `gs`, `GlideSystem`, workflow APIs, and everything else.

```
$ snow run hello.js
Hello from admin

$ snow run --auto-login fix_stale_records.js
Records updated: 42

$ echo "gs.info('Hello from ' + gs.getUserName());" | snow run
Hello from admin
```

**Query tables:**

```
$ snow record search -q "active=true^priority=1" -f number,short_description -l 5 incident
number    short_description
--------  -----------------------------------------
INC00042  VPN connectivity failure
INC00051  Email service degraded
INC00078  Database backup failing on primary node
INC00091  Load balancer health check timeout
INC00103  API gateway returning 502 errors
```

**Aggregate data:**

```
$ snow record aggregate --count -g priority incident
priority    count
----------  -------
1 - Critical        12
2 - High           47
3 - Moderate      213
4 - Low           891
```

**Inspect table schemas:**

```
$ snow table fields incident
field              label                    type             references
-----------------  -----------------------  ---------------  ----------------
assigned_to        Assignee                 reference        sys_user
category           Category                 string
caused_by          Caused by Change         reference        change_request
location           Location                 reference        cmn_location
priority           Priority                 integer
state              State                    integer
sys_updated_on     Updated                  glide_date_time
```

---

## Use Cases

### ServiceNow developer: automate script execution

Run scripts as part of your development workflow, with clean stdout separated from ServiceNow wrapper noise.

```
$ snow run scripts/update_assignments.js
Updated 17 records.

$ snow run --auto-login scripts/update_assignments.js   # retry with login+elevate if session expired
Updated 17 records.
```

Auto-login retries once with `login` + `elevate` when the session has expired — no manual intervention needed in CI or scheduled jobs.

### Admin / platform engineer: export and document schemas

Dump table schemas for documentation, change review, or data mapping.

```
$ snow table fields cmdb_ci_server -F excel -O cmdb_ci_server_fields.xlsx
$ snow table fields task -F csv -O task_fields.csv
$ snow record search -q "active=true" -F json -O open_incidents.json incident
```

Count records to validate data quality checks:

```
$ snow record count -q "active=true^assigned_toISEMPTY" incident
134
```

### AI / LLM user: connect Claude or Copilot to your instance

After adding the MCP server config, your AI assistant can query, aggregate, and inspect ServiceNow data directly. No scripting required — just ask.

**Claude Desktop config** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "servicenow": {
      "command": "snow",
      "args": ["mcp"]
    }
  }
}
```

**MCP tools exposed:**

| Tool | Description |
|---|---|
| `snow_run_script` | Execute a JavaScript background script |
| `snow_login` | Log in and persist the session cookie |
| `snow_elevate` | Elevate to the `security_admin` role |
| `snow_list_instances` | List configured instances |
| `snow_record_count` | Count records matching an optional query |
| `snow_table_fields` | List all fields (including inherited) for a table |
| `snow_record_search` | Query table records with filtering, sorting, projection |
| `snow_record_aggregate` | Aggregate records (count/avg/sum/min/max with group-by) |

All tools accept an optional `instance` argument; omit it to use the default.

> **Context window tip:** `snow_record_search` and `snow_table_fields` return data inline by default. Specify `limit` for record searches. For large tables, pass `output_file` to save to disk — the tool returns `{"saved_to": "...", "count": N}` instead of the full payload.

### Multi-instance: manage dev, test, and prod

```
$ snow add --default dev1234.service-now.com
$ snow add test5678.service-now.com

$ snow list
* dev1234.service-now.com (default)
  test5678.service-now.com

$ snow use test5678.service-now.com
$ snow -i dev1234.service-now.com run script.js   # one-off override
```

---

## Quickstart

> ⚠️ Do not run this against a production instance.

**Install with uv (recommended):**

```bash
uv tool install git+https://github.com/chovanecm/snow-cli@main
```

**Add your instance and run:**

```bash
snow add --default dev1234.service-now.com
snow login
echo "gs.print('Hello');" | snow run
```

<details>
<summary>Other install methods</summary>

**Try without installing (uv):**
```bash
uvx --from git+https://github.com/chovanecm/snow-cli@main snow --help
```

**Install via pipx:**
```bash
pipx install git+https://github.com/chovanecm/snow-cli@main
# upgrade later
pipx upgrade --spec git+https://github.com/chovanecm/snow-cli@main snow
```

**Install via pip:**
```bash
python3 -m pip install --user git+https://github.com/chovanecm/snow-cli@main
```

**Pin to a specific tag or commit:**
```bash
pipx install "git+https://github.com/chovanecm/snow-cli@<tag_or_commit>"
```

**Development install:**
```bash
git clone https://github.com/chovanecm/snow-cli.git
cd snow-cli
pip install -e .
```

</details>

---

## Commands

- `snow add [--default] [INSTANCE]` — Add an instance (interactive credentials)
- `snow list` — List configured instances
- `snow use INSTANCE` — Set the default instance
- `snow remove INSTANCE` — Remove an instance
- `snow info` — Show current configuration and paths
- `snow login` — Login and persist session cookies
- `snow elevate` — Elevate to `security_admin`
- `snow run [--auto-login] [SCRIPT_FILE|-]` — Run a Background Script (file or stdin)
- `snow record search [options] TABLE_NAME` — Query table records
- `snow record count [-q QUERY] TABLE_NAME` — Count matching records
- `snow record aggregate [options] TABLE_NAME` — Aggregate records
- `snow r search / snow r count / snow r a` — Short aliases for record commands
- `snow table fields [options] TABLE_NAME` — List all fields with labels and types
- `snow mcp` — Start the MCP server (stdio) for AI assistant integration

---

## Record Queries

### Counting records

```bash
snow record count incident
snow record count -q "active=true" incident
snow r count -q "sys_created_on>=2024-01-01" incident
```

Prints just the integer count — no headers, no formatting.

### Aggregating records

Use `snow record aggregate` (or alias `snow r a`) to run aggregate queries via the [ServiceNow Aggregate API](https://docs.servicenow.com/bundle/zurich-api-reference/page/integrate/inbound-rest/concept/c_AggregateAPI.html).

At least one aggregate function must be specified.

```bash
# Count all incidents
snow record aggregate --count incident

# Count grouped by priority
snow record aggregate --count -g priority incident

# Count + average reassignment_count grouped by state, filtered to active
snow record aggregate --count --avg reassignment_count -g state -q "active=true" incident

# Only groups with more than 5 records
snow r a --count -g category --having "COUNT>5" incident

# Multiple functions in JSON
snow r a --count --min opened_at --max closed_at -g priority -F json incident
```

**Options:**

| Option | Description |
|--------|-------------|
| `--count` | Include COUNT in results |
| `--avg FIELD` | Include AVG of field (repeatable) |
| `--sum FIELD` | Include SUM of field (repeatable) |
| `--min FIELD` | Include MIN of field (repeatable) |
| `--max FIELD` | Include MAX of field (repeatable) |
| `-g, --group-by FIELD` | Group results by field (repeatable) |
| `-q, --query TEXT` | Encoded query filter (sysparm_query) |
| `--having TEXT` | HAVING clause (e.g. `COUNT>10`) |
| `--display-values` | `values`/`display`/`both` (default `both`) |
| `-F, --format` | `table` *(default)*, `tsv`, `csv`, `json` |
| `-O, --output FILE` | Write output to file |

### Searching records

Use `snow record search` (or alias `snow r search`) to query ServiceNow tables.

```bash
# Default output is a pretty table
snow record search incident

# Filter + projection + limit
snow record search -q "active=true^priority=1" -f number,short_description,state -l 10 incident

# Sorting
snow record search -o number -od sys_created_on incident

# sys_id only
snow record search --sys-id incident

# Display mode: values|display|both (default both)
snow record search --display-values both -f caller_id,assignment_group incident
```

### Output formats

| Format  | Description                                           |
|---------|-------------------------------------------------------|
| `table` | *(default)* Pretty-printed aligned table              |
| `tsv`   | Tab-separated values                                  |
| `csv`   | Comma-separated values                                |
| `json`  | JSON array (full `value`/`display_value` objects)     |
| `xml`   | ServiceNow XML unload format (`<unload>…</unload>`)   |
| `excel` | Excel `.xlsx` — requires `-O/--output FILE`           |

```bash
snow record search -l 20 -f number,state -F csv incident
snow record search -l 100 -f number,short_description -F json -O incidents.json incident
snow record search -q "active=true" -F excel -O open_incidents.xlsx incident
```

### All search options

- `-q, --query ENCODED_QUERY`
- `-o, --order-by FIELD` (repeatable)
- `-od, --order-by-desc FIELD` (repeatable)
- `-f, --fields FIELDS` (comma-separated)
- `-l, --limit N`
- `-F, --format [table|tsv|csv|json|xml|excel]` (default: `table`)
- `-O, --output FILE`
- `--no-header`
- `--sys-id` (shortcut for `-f sys_id --no-header`)
- `--display-values [values|display|both]` (default: `both`)

---

## Table Schema

Inspect all columns on a table, including those inherited from parent tables.

```bash
# Pretty table (default)
snow table fields incident

# JSON (includes reference targets)
snow table fields incident -F json

# CSV for documentation
snow table fields cmdb_ci -F csv -O cmdb_ci_fields.csv

# Excel
snow table fields task -F excel -O task_fields.xlsx
```

### Example output

```
field              label                    type             references
-----------------  -----------------------  ---------------  ----------------
assigned_to        Assignee                 reference        sys_user
caused_by          Caused by Change         reference        change_request
location           Location                 reference        cmn_location
made_sla           Made SLA                 boolean
parent             Parent                   reference        task
sys_updated_on     Updated                  glide_date_time
watch_list         Watch list               glide_list
```

**Options:** `-F [table|tsv|csv|json|xml|excel]`, `-O FILE`

---

## Configuration

Use `snow add` to store credentials securely (OS keyring when available) and set a default instance. Override with `--instance` for one-off commands. Config stored in `~/.snow-run/config.json` (permissions: 600). Passwords go to the OS keyring when available, config file otherwise.

```bash
snow add dev1234.service-now.com
snow add --default dev5678.service-now.com
snow list
snow use dev5678.service-now.com
snow remove dev1234.service-now.com
snow info
```

### OAuth sign-in

An instance can use OAuth instead of a stored username and password. You sign in through the browser, and calls run as you, with your roles. Single sign-on works if the instance uses it.

```bash
snow add --oauth dev1234.service-now.com   # prompts for client ID and secret
snow -i dev1234.service-now.com login      # opens the browser
snow -i dev1234.service-now.com r search sys_db_object -l 1
```

**Instance setup:** Create an OAuth client for external clients, under System OAuth > Application Registry, or All > Machine Identity Console on newer releases. Add `http://localhost:8765/callback` as its Redirect URL. No system property is needed. The scope granted to the token comes from the client.

| Feature | Under OAuth |
|---|---|
| `snow record`, `snow r`, `snow table` | Works |
| MCP search, count, aggregate and fields tools | Works after `snow login` |
| `snow_login` MCP tool | Not available: run `snow login` in a terminal |
| `snow run`, `snow elevate`, `snow_run_script`, `snow_elevate` | Not available: these need a UI session, which OAuth sign-in does not create |

Access tokens renew on their own. When renewal is no longer possible, commands tell you to run `snow login` again. A person must sign in at least once, so OAuth cannot bootstrap unattended runs.

`snow_client_id` and `snow_client_secret` supply the client from the environment. They must be set together. Credentials resolve in this order: those two variables, then `snow_user` with `snow_pwd`, then the stored instance.

---

## Working with Instances

```bash
snow login                                          # default instance
snow -i dev5678.service-now.com login               # specific instance

snow run script.js
snow run --auto-login script.js
echo "gs.print('Hello');" | snow run

snow -i dev5678.service-now.com run script.js
snow elevate
snow -i dev5678.service-now.com elevate
```

---

## Troubleshooting & Debugging

- Last raw HTML output after running scripts: `~/.snow-run/tmp/{instance}/last_run_output.txt`
- `snow run` wraps each script with generated boundary markers so ServiceNow wrapper text stays on stderr instead of polluting stdout
- `snow run --auto-login` retries once with `login` + `elevate` when the failure is "Cannot get security token..."; auto-auth progress is on stderr
- On errors, the CLI prints clear messages and relevant HTTP status codes

---

## Development

```bash
pip install -e .
PYTHONPATH=. python3 -m unittest discover -s tests -q
```
