"""Command-line interface for ServiceNow CLI"""
import sys
import click
from .instance_repository import InstanceRepository
from .commands import login, elevate, run_script, search_records, table_fields, count_records, aggregate_records, OutputFormat
from .instance_manager import add_instance, add_oauth_instance, list_instances, use_instance, remove_instance, show_info

@click.group()
@click.option(
    "-i", "--instance",
    envvar="snow_instance",
    help="ServiceNow instance (overrides default)",
)
@click.pass_context
def main(ctx, instance):
    """ServiceNow CLI - Platform-independent command-line interface

    Manage multiple ServiceNow instances:
      snow add                    # Add a new instance
      snow list                   # List all instances
      snow use <instance>         # Set default instance
      snow login                  # Login to default instance
      snow --instance dev2 login  # Login to specific instance
    """
    # Initialize config with specified instance
    try:
        config = InstanceRepository().load_config(instance=instance)
    except ValueError as e:
        raise click.ClickException(str(e))
    ctx.obj = config


@main.command(name="login")
@click.pass_obj
def login_cmd(config):
    """Login to ServiceNow instance and save session"""
    sys.exit(login(config))


@main.command(name="elevate")
@click.pass_obj
def elevate_cmd(config):
    """Elevate user privileges (security_admin role)"""
    sys.exit(elevate(config))


@main.command()
@click.argument("script_file", required=False)
@click.option("--auto-login", is_flag=True, help="If script token acquisition fails, run login + elevate and retry once")
@click.pass_obj
def run(config, script_file, auto_login):
    """Run a background script on ServiceNow

    SCRIPT_FILE: Path to JavaScript file to execute (or '-' for stdin)

    Examples:
      snow run example.js
      snow run --auto-login example.js
      echo "gs.print('Hello');" | snow run
      snow run < script.js
    """
    sys.exit(run_script(config, script_file, auto_login=auto_login))


@main.command()
@click.argument("instance", required=False)
@click.option("--default", is_flag=True, help="Set as default instance")
@click.option("--oauth", is_flag=True, help="Sign in through OAuth instead of a username and password")
@click.option("--client-id", help="OAuth client ID (prompted for if omitted). The secret is always prompted for.")
def add(instance, default, oauth, client_id):
    """Add a new ServiceNow instance

    INSTANCE: ServiceNow instance (e.g. dev1234.service-now.com)

    Examples:
      snow add dev1234.service-now.com
      snow add --default dev1234.service-now.com
      snow add  # Interactive prompt
      snow add --oauth dev1234.service-now.com
    """
    if client_id and not oauth:
        raise click.UsageError("--client-id requires --oauth")
    if oauth:
        sys.exit(add_oauth_instance(instance, client_id, default))
    sys.exit(add_instance(instance, default))


@main.command("list")
def list_cmd():
    """List all configured instances"""
    sys.exit(list_instances())


@main.group()
def record():
    """Record operations."""


@main.group(name="r")
def r_alias():
    """Alias for record."""


_FORMAT_OPTION = [
    click.option(
        "-F", "--format", "fmt",
        type=click.Choice(list(OutputFormat), case_sensitive=False),
        default="table",
        show_default=True,
        help="Output format",
    ),
    click.option("-O", "--output", "output_file", default=None, help="Write output to FILE (required for excel)"),
]


def _add_format_options(func):
    for option in reversed(_FORMAT_OPTION):
        func = option(func)
    return func


_AGGREGATE_FORMAT_OPTION = [
    click.option(
        "-F", "--format", "fmt",
        type=click.Choice(OutputFormat.aggregate_choices(), case_sensitive=False),
        default="table",
        show_default=True,
        help="Output format",
    ),
    click.option("-O", "--output", "output_file", default=None, help="Write output to FILE"),
]


def _add_aggregate_format_options(func):
    for option in reversed(_AGGREGATE_FORMAT_OPTION):
        func = option(func)
    return func


@click.command(name="search")
@click.option("-q", "--query", "query", help="Encoded query (sysparm_query)")
@click.option("-o", "--order-by", "order_by", multiple=True, help="Order by field (can be used multiple times)")
@click.option("-od", "--order-by-desc", "order_by_desc", multiple=True, help="Order by field descending (can be used multiple times)")
@click.option("-f", "--fields", help="Comma-separated list of fields to return")
@click.option("-l", "--limit", type=int, help="Maximum number of records to return")
@click.option("--no-header", is_flag=True, help="Omit column headers")
@click.option("--sys-id", "sys_id", is_flag=True, help="Shortcut for -f sys_id --no-header")
@click.option(
    "--display-values",
    type=click.Choice(["values", "display", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Return field values, display values, or both",
)
@_add_format_options
@click.argument("table_name")
@click.pass_obj
def search_cmd(config, query, order_by, order_by_desc, fields, limit, no_header, sys_id, display_values, fmt, output_file, table_name):
    """Perform a query on a table."""
    sys.exit(
        search_records(
            config=config,
            table=table_name,
            query=query,
            order_by=list(order_by),
            order_by_desc=list(order_by_desc),
            fields=fields,
            limit=limit,
            no_header=no_header,
            sys_id=sys_id,
            display_values=display_values.lower(),
            fmt=fmt.lower(),
            output_file=output_file,
        )
    )


@click.command(name="count")
@click.option("-q", "--query", "query", help="Encoded query to filter records (sysparm_query)")
@click.argument("table_name")
@click.pass_obj
def count_cmd(config, query, table_name):
    """Count records in a table, optionally filtered by a query.

    Prints just the integer count.

    Examples:
      snow record count incident
      snow record count -q "active=true" incident
      snow record count -q "sys_created_on>=2024-01-01" incident
    """
    sys.exit(count_records(config, table_name, query=query))


@click.command(name="aggregate")
@click.option("-q", "--query", "query", help="Encoded query to filter records (sysparm_query)")
@click.option("-g", "--group-by", "group_by", multiple=True, help="Field to group by (can be used multiple times)")
@click.option("--count", "count", is_flag=True, help="Include COUNT in results")
@click.option("--avg", "avg", multiple=True, metavar="FIELD", help="Include AVG of FIELD (can be used multiple times)")
@click.option("--sum", "sum_fields", multiple=True, metavar="FIELD", help="Include SUM of FIELD (can be used multiple times)")
@click.option("--min", "min_fields", multiple=True, metavar="FIELD", help="Include MIN of FIELD (can be used multiple times)")
@click.option("--max", "max_fields", multiple=True, metavar="FIELD", help="Include MAX of FIELD (can be used multiple times)")
@click.option("--having", "having", help="HAVING clause filter (sysparm_having, e.g. COUNT>10)")
@click.option(
    "--display-values",
    type=click.Choice(["values", "display", "both"], case_sensitive=False),
    default="both",
    show_default=True,
    help="Return field values, display values, or both",
)
@_add_aggregate_format_options
@click.argument("table_name")
@click.pass_obj
def aggregate_cmd(config, query, group_by, count, avg, sum_fields, min_fields, max_fields,
                  having, display_values, fmt, output_file, table_name):
    """Aggregate records in a table using the ServiceNow Aggregate API.

    At least one of --count, --avg, --sum, --min, or --max must be provided.

    Examples:
      snow record aggregate --count incident
      snow record aggregate --count -g priority incident
      snow record aggregate --count -g category -q "active=true" incident
      snow record aggregate --count --avg reassignment_count -g priority incident
      snow r a --count -g state problem
    """
    sys.exit(
        aggregate_records(
            config=config,
            table=table_name,
            query=query,
            group_by=list(group_by) if group_by else None,
            count=count,
            avg=list(avg) if avg else None,
            sum_fields=list(sum_fields) if sum_fields else None,
            min_fields=list(min_fields) if min_fields else None,
            max_fields=list(max_fields) if max_fields else None,
            having=having,
            display_values=display_values.lower(),
            fmt=fmt.lower(),
            output_file=output_file,
        )
    )


record.add_command(search_cmd)
record.add_command(count_cmd)
record.add_command(aggregate_cmd)

r_alias.add_command(search_cmd)
r_alias.add_command(count_cmd)
r_alias.add_command(aggregate_cmd)
r_alias.add_command(aggregate_cmd, name="a")



@main.group(name="table")
def table_group():
    """Table schema operations."""


@table_group.command(name="fields")
@click.argument("table_name")
@_add_format_options
@click.pass_obj
def table_fields_cmd(config, table_name, fmt, output_file):
    """List all fields (including inherited) for a ServiceNow table.

    Outputs field name, label, type, and referenced table (for reference fields).

    Examples:
      snow table fields incident
      snow table fields incident -F json
      snow table fields cmdb_ci -F csv -O cmdb_ci_fields.csv
      snow table fields task -F excel -O task_fields.xlsx
    """
    sys.exit(table_fields(config, table_name, fmt=fmt.lower(), output_file=output_file))


@main.command()
@click.argument("instance")
def use(instance):
    """Set default instance

    Examples:
      snow use dev1234.service-now.com
    """
    sys.exit(use_instance(instance))


@main.command()
@click.argument("instance")
def remove(instance):
    """Remove an instance

    Examples:
      snow remove dev1234.service-now.com
    """
    sys.exit(remove_instance(instance))


@main.command()
def info():
    """Show current configuration and instances"""
    sys.exit(show_info())


@main.command()
def mcp():
    """Start the MCP server (stdio transport) for AI assistant integration

    Exposes snow_run_script, snow_login, snow_elevate, snow_list_instances, and snow_record_search as MCP tools.

    Example Claude Desktop config (~/.config/claude/claude_desktop_config.json):

    \b
    {
      "mcpServers": {
        "servicenow": {
          "command": "snow",
          "args": ["mcp"]
        }
      }
    }
    """
    from .mcp_server import serve
    serve()


if __name__ == "__main__":
    main()
