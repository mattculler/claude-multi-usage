"""CLI entry point for claude-multi-usage."""

from __future__ import annotations

from datetime import datetime

import click

from .parser import load_usage_data, parse_today_usage, HourlyUsage, UsageData
from .dashboard import render_dashboard, render_multi_device_dashboard, make_projects_table, make_models_table, make_today_hourly_chart, Console, format_tokens
from .cost_cache import get_costs
from .pricing import format_cost


CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


def _validate_date(ctx, param, value):
    """Click callback: require YYYY-MM-DD so bad input is a usage error, not a traceback."""
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise click.BadParameter("expected a date in YYYY-MM-DD format")
    return value


def _build_device_usage_data(device: dict) -> UsageData:
    """Convert a single device dict (from server API) into UsageData."""
    from .parser import DailyActivity, DailyModelTokens, ModelUsage, ProjectSummary

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    usage_data = UsageData(
        hostname=device["hostname"],
        daily_activity=[
            DailyActivity(
                date=a["date"],
                message_count=a["message_count"],
                session_count=a["session_count"],
                tool_call_count=a["tool_call_count"],
            )
            for a in device.get("daily_activity", [])
        ],
        daily_model_tokens=[
            DailyModelTokens(date=t["date"], tokens_by_model=t["tokens_by_model"])
            for t in device.get("daily_model_tokens", [])
        ],
        model_usage=[
            ModelUsage(
                model=m["model"],
                input_tokens=m["input_tokens"],
                output_tokens=m["output_tokens"],
                cache_read_tokens=m["cache_read_tokens"],
                cache_creation_tokens=m["cache_creation_tokens"],
            )
            for m in device.get("model_usage", [])
        ],
        projects=sorted([
            ProjectSummary(
                name=p["name"],
                session_count=p["session_count"],
                output_tokens=p.get("output_tokens", 0),
                input_tokens=p.get("input_tokens", 0),
                first_seen=_parse_dt(p.get("first_seen")),
                last_seen=_parse_dt(p.get("last_seen")),
            )
            for p in device.get("projects", [])
        ], key=lambda p: p.output_tokens, reverse=True),
        hour_counts={int(k): v for k, v in device.get("hour_counts", {}).items()},
        total_sessions=device.get("total_sessions", 0),
        total_messages=device.get("total_messages", 0),
        first_session_date=device.get("first_session_date"),
        alias=device.get("alias"),
        today_hourly=[
            HourlyUsage(hour=h["hour"], message_count=h["message_count"],
                        session_count=h["session_count"], tokens=h["tokens"])
            for h in device.get("today_hourly", [])
        ],
        today_hourly_date=device.get("today_hourly_date"),
    )
    return usage_data


def _merge_devices_usage(devices: list[dict]) -> UsageData:
    """Merge all device dicts into a single aggregated UsageData."""
    from .parser import DailyActivity, DailyModelTokens, ModelUsage, ProjectSummary

    all_activity: dict[str, DailyActivity] = {}
    all_tokens: dict[str, dict[str, int]] = {}
    all_model_usage: dict[str, list[int]] = {}
    all_projects: dict[str, list] = {}
    all_hour_counts: dict[int, int] = {}
    total_sessions = 0
    total_messages = 0
    hostnames = []
    first_dates = []

    for device in devices:
        hostnames.append(device["hostname"])
        total_sessions += device.get("total_sessions", 0)
        total_messages += device.get("total_messages", 0)
        if device.get("first_session_date"):
            first_dates.append(device["first_session_date"])

        for a in device.get("daily_activity", []):
            d = a["date"]
            if d in all_activity:
                existing = all_activity[d]
                all_activity[d] = DailyActivity(
                    date=d,
                    message_count=existing.message_count + a["message_count"],
                    session_count=existing.session_count + a["session_count"],
                    tool_call_count=existing.tool_call_count + a["tool_call_count"],
                )
            else:
                all_activity[d] = DailyActivity(
                    date=d,
                    message_count=a["message_count"],
                    session_count=a["session_count"],
                    tool_call_count=a["tool_call_count"],
                )

        for t in device.get("daily_model_tokens", []):
            d = t["date"]
            if d not in all_tokens:
                all_tokens[d] = {}
            for model, count in t["tokens_by_model"].items():
                all_tokens[d][model] = all_tokens[d].get(model, 0) + count

        for m in device.get("model_usage", []):
            model = m["model"]
            if model not in all_model_usage:
                all_model_usage[model] = [0, 0, 0, 0]
            all_model_usage[model][0] += m["input_tokens"]
            all_model_usage[model][1] += m["output_tokens"]
            all_model_usage[model][2] += m["cache_read_tokens"]
            all_model_usage[model][3] += m["cache_creation_tokens"]

        for p in device.get("projects", []):
            name = p["name"]
            if name not in all_projects:
                all_projects[name] = [0, 0, 0, None, None]
            all_projects[name][0] += p["session_count"]
            all_projects[name][1] += p.get("output_tokens", 0)
            all_projects[name][2] += p.get("input_tokens", 0)
            fs = p.get("first_seen")
            ls = p.get("last_seen")
            if fs:
                cur_first = all_projects[name][3]
                if cur_first is None or fs < cur_first:
                    all_projects[name][3] = fs
            if ls:
                cur_last = all_projects[name][4]
                if cur_last is None or ls > cur_last:
                    all_projects[name][4] = ls

        for h_str, count in device.get("hour_counts", {}).items():
            h = int(h_str)
            all_hour_counts[h] = all_hour_counts.get(h, 0) + count

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    hostname_label = " + ".join(sorted(hostnames)) if len(hostnames) <= 3 else f"{len(hostnames)} devices"

    return UsageData(
        hostname=hostname_label,
        daily_activity=sorted(all_activity.values(), key=lambda a: a.date),
        daily_model_tokens=[
            DailyModelTokens(date=d, tokens_by_model=t)
            for d, t in sorted(all_tokens.items())
        ],
        model_usage=[
            ModelUsage(model=m, input_tokens=v[0], output_tokens=v[1],
                       cache_read_tokens=v[2], cache_creation_tokens=v[3])
            for m, v in all_model_usage.items()
        ],
        projects=sorted([
            ProjectSummary(name=n, session_count=v[0], output_tokens=v[1],
                           input_tokens=v[2], first_seen=_parse_dt(v[3]),
                           last_seen=_parse_dt(v[4]))
            for n, v in all_projects.items()
        ], key=lambda p: p.output_tokens, reverse=True),
        hour_counts=all_hour_counts,
        total_sessions=total_sessions,
        total_messages=total_messages,
        first_session_date=min(first_dates) if first_dates else None,
    )


def _fetch_all_devices(key: str | None = None) -> list[dict]:
    """Fetch raw device data list from the sync server."""
    import json
    import urllib.request
    import urllib.error
    import urllib.parse
    from .config import get_server_url, get_keys

    console = Console()
    server_url = get_server_url()

    if not server_url:
        console.print("[red]Server URL not configured.[/red]")
        console.print("Run: cmu config server <url>")
        raise SystemExit(1)

    keys = get_keys()
    if not keys:
        console.print("[red]No keys configured.[/red]")
        console.print("Run: cmu config key add <your-key>")
        raise SystemExit(1)

    # Query only the given key, otherwise every registered key
    query_keys = [key] if key else [k["key"] for k in keys]

    all_devices: dict[str, dict] = {}  # hostname -> device data (deduplicated)
    for qk in query_keys:
        try:
            params = urllib.parse.urlencode({"key": qk})
            req = urllib.request.Request(
                f"{server_url}/api/usage?{params}",
                headers={"User-Agent": "cmu"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                devices = json.loads(resp.read())
                for d in devices:
                    all_devices[d["hostname"]] = d
        except urllib.error.URLError as e:
            console.print(f"[red]Failed to fetch from server:[/red] {e}")
            raise SystemExit(1)

    if not all_devices:
        console.print("[dim]No device data on server.[/dim]")
        raise SystemExit(0)

    return list(all_devices.values())


@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="claude-multi-usage")
@click.pass_context
def main(ctx):
    """Claude Code usage dashboard across multiple devices.

    \b
    Quick start:
      cmu                        Full dashboard (last 14 days)
      cmu today                  Today's realtime usage
      cmu hourly                 Today's hourly breakdown
      cmu projects               Project breakdown with token usage
      cmu projects -n 5          Top 5 projects only
      cmu models                 Model usage breakdown
      cmu cost                   Monthly cost breakdown
      cmu dashboard -d 7         Last 7 days
      cmu dashboard -d 30        Last 30 days
      cmu dashboard --from 2026-03-01 --to 2026-03-07    Date range
      cmu diff                   All devices (per-device view)
      cmu diff --merged          All devices (merged view)
      cmu sync                   Push local usage to the sync server
      cmu autosync install       Keep the server updated automatically
      cmu remote install HOST    Set up cmu + autosync on another machine
      cmu import-claude-export   Import a claude.ai data export

    \b
    Data source:
      Reads ~/.claude/stats-cache.json and session .jsonl files.
      No API keys required. Network is used only to fetch model pricing
      from LiteLLM's GitHub repo (cached 24h), for the opt-in sync
      commands (sync, autosync, import-claude-export) and for
      `cmu remote` over your own ssh.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(dashboard)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--days", "-d", default=14, show_default=True, type=click.IntRange(min=1),
              help="Number of recent days to display in the chart.")
@click.option("--from", "date_from", default=None, metavar="YYYY-MM-DD",
              callback=_validate_date, help="Start date for the chart range.")
@click.option("--to", "date_to", default=None, metavar="YYYY-MM-DD",
              callback=_validate_date, help="End date for the chart range.")
def dashboard(days: int, date_from: str, date_to: str):
    """Show the full usage dashboard.

    \b
    Examples:
      cmu dashboard                 Last 14 days (default)
      cmu dashboard -d 7            Last 7 days
      cmu dashboard -d 30           Last 30 days

    \b
    Includes: summary, model usage, daily token chart,
    hourly heatmap, and top projects.
    """
    data = load_usage_data()
    render_dashboard(data, days=days, date_from=date_from, date_to=date_to)


@main.command(context_settings=CONTEXT_SETTINGS)
def today():
    """Show today's usage in realtime.

    \b
    Uses the session index (see `cmu cost --help`), which picks up
    new session lines on every run, so it works even before
    stats-cache.json is updated by Claude Code.

    \b
    Shows: sessions, messages, tool calls, tokens by model.
    """
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    data = load_usage_data()
    today_str = datetime.now().strftime("%Y-%m-%d")

    activity = next((a for a in data.daily_activity if a.date == today_str), None)
    tokens_data = next((t for t in data.daily_model_tokens if t.date == today_str), None)

    if not activity:
        result = parse_today_usage()
        if result:
            activity, tokens_data = result

    if not activity:
        console.print(f"[dim]No usage data for today ({today_str})[/dim]")
        return

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", style="dim")
    table.add_column("value", style="bold cyan")

    table.add_row("Date", today_str)
    table.add_row("Sessions", str(activity.session_count))
    table.add_row("Messages", str(activity.message_count))
    table.add_row("Tool Calls", str(activity.tool_call_count))

    if tokens_data:
        for model, tokens in tokens_data.tokens_by_model.items():
            short = model.split("-202")[0] if "-202" in model else model
            table.add_row(f"Tokens ({short})", format_tokens(tokens))
        table.add_row("Total Tokens", format_tokens(tokens_data.total_tokens))

    # Today's cost
    from .cost_cache import day_cost
    from .pricing import get_pricing
    if get_pricing() is not None:
        table.add_row("Cost", f"[bold yellow]{format_cost(day_cost(today_str))}[/bold yellow]")

    console.print()
    console.print(Panel(table, title=f"Today - {data.hostname}", border_style="blue"))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
def hourly():
    """Show today's usage broken down by hour.

    \b
    Shows per-hour token usage, message counts, and session counts
    for today from the session index (see `cmu cost --help`).

    \b
    Shows: 24-hour bar chart with tokens, messages, sessions per hour.
    Current hour is marked with *.
    """
    from .parser import parse_today_hourly

    console = Console()
    hourly_data = parse_today_hourly()

    if not any(h.tokens > 0 or h.message_count > 0 for h in hourly_data):
        console.print("[dim]No usage data for today.[/dim]")
        return

    console.print()
    console.print(make_today_hourly_chart(hourly_data))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--limit", "-n", default=20, show_default=True,
              help="Number of projects to display.")
def projects(limit: int):
    """Show project usage breakdown sorted by output tokens.

    \b
    Per-repository token usage from the session index (see
    `cmu cost --help`). Shows sessions, output tokens, cost, and
    last used date.
    """
    console = Console()
    data = load_usage_data()
    console.print()
    console.print(make_projects_table(data, limit=limit))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
def models():
    """Show model usage breakdown.

    \b
    Displays output tokens, cache read, and cache creation
    for each model (e.g., claude-opus-4-6, claude-sonnet-4-5).
    """
    console = Console()
    data = load_usage_data()
    console.print()
    console.print(make_models_table(data))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--rebuild", is_flag=True,
              help="Re-read every session file on disk (needed after changing the "
                   "system time zone). History of transcripts Claude Code has since "
                   "deleted is kept.")
def cost(rebuild: bool):
    """Show monthly cost breakdown.

    \b
    Token counts come from an incremental index of the session files
    (~/.claude-multi-usage/index.db): only files that changed since the
    last run are read, and transcripts Claude Code deletes stay in the
    index as history. Prices are applied when displaying, so a pricing
    update takes effect immediately.

    \b
    Pricing is fetched from LiteLLM's pricing DB and cached locally.
    Falls back to last cached pricing if network is unavailable.
    """
    from collections import defaultdict
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    if rebuild:
        from .index import rebuild as rebuild_index
        stats = rebuild_index()
        console.print(f"[dim]Session index rebuilt: {stats.file_count(include_gone=False)} files re-read.[/dim]")
    costs = get_costs()
    if costs is None:
        console.print()
        console.print("[bold red]Pricing data unavailable.[/bold red]")
        console.print("[dim]Run with network access to fetch pricing from LiteLLM.[/dim]")
        console.print()
        return
    daily_costs, total_cost, today_cost = costs

    monthly = defaultdict(lambda: defaultdict(float))
    for date_str, models in sorted(daily_costs.items()):
        month = date_str[:7]
        for model, info in models.items():
            short = model.split("-202")[0] if "-202" in model else model
            monthly[month][short] += info["cost"]

    table = Table(box=None, padding=(0, 1))
    table.add_column("Month", style="bold")
    table.add_column("Model", style="dim")
    table.add_column("Cost", justify="right", style="bold yellow")

    for month in sorted(monthly.keys()):
        models = monthly[month]
        first = True
        month_total = sum(models.values())
        for model in sorted(models.keys(), key=lambda m: models[m], reverse=True):
            table.add_row(
                month if first else "",
                model,
                format_cost(models[model]),
            )
            first = False
        table.add_row("", "[bold]subtotal[/bold]", f"[bold]{format_cost(month_total)}[/bold]")
        table.add_row("", "", "")

    table.add_row("[bold]Total[/bold]", "", f"[bold yellow]{format_cost(total_cost)}[/bold yellow]")

    console.print()
    console.print(Panel(table, title="Cost Breakdown (by month)", border_style="yellow"))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--days", "-d", default=14, show_default=True, type=click.IntRange(min=1),
              help="Number of recent days to display in the chart.")
@click.option("--from", "date_from", default=None, metavar="YYYY-MM-DD",
              callback=_validate_date, help="Start date for the chart range.")
@click.option("--to", "date_to", default=None, metavar="YYYY-MM-DD",
              callback=_validate_date, help="End date for the chart range.")
@click.option("--merged", is_flag=True,
              help="Merge all devices into one view.")
@click.option("--key", "filter_key", default=None, metavar="KEY",
              help="Filter by specific key.")
def diff(days: int, date_from: str, date_to: str, merged: bool,
         filter_key: str):
    """Show multi-device usage from the sync server.

    \b
    Examples:
      cmu diff                       All devices (per-device view)
      cmu diff --merged              All devices (merged view)
      cmu diff --key team-key        Specific key
      cmu diff -d 30                 Last 30 days

    \b
    Includes: summary, model usage, daily token chart, hourly usage for
    the day each device last synced, and top projects. Cost estimates are
    not shown: the server holds token counts only.
    """
    devices = _fetch_all_devices(key=filter_key)

    if merged:
        data = _merge_devices_usage(devices)
        render_dashboard(data, days=days, date_from=date_from, date_to=date_to, local=False)
    else:
        devices_data = [_build_device_usage_data(d) for d in devices]
        render_multi_device_dashboard(devices_data, days=days, date_from=date_from, date_to=date_to)


@main.group(context_settings=CONTEXT_SETTINGS)
def config():
    """Configure claude-multi-usage settings.

    \b
    Examples:
      cmu config server https://your-server.com
      cmu config key add my-key "description"
      cmu config key remove my-key
      cmu config key list
      cmu config show
    """
    pass


@config.command(name="server")
@click.argument("url")
def config_server(url: str):
    """Set the sync server URL.

    \b
    Example:
      cmu config server https://your-server.com
    """
    from .config import set_server_url

    console = Console()
    set_server_url(url)
    console.print(f"[green]Server URL set to:[/green] {url}")


@config.command(name="show")
def config_show():
    """Show current configuration."""
    from .config import load_config

    console = Console()
    cfg = load_config()
    console.print()
    console.print("[bold]Current configuration:[/bold]")
    console.print(f"  server_url: {cfg.get('server_url') or '[dim]not set[/dim]'}")
    console.print(f"  alias:      {cfg.get('alias') or '[dim]not set[/dim]'}")
    keys = cfg.get("keys", [])
    if keys:
        console.print("  keys:")
        for k in keys:
            desc = k.get("description", "")
            desc_str = f"  [dim]{desc}[/dim]" if desc else ""
            console.print(f"    - {k['key']}{desc_str}")
    else:
        console.print("  keys:       [dim]not set[/dim]")
    console.print()


@config.command(name="alias")
@click.argument("name")
def config_alias(name: str):
    """Set this device's display alias.

    \b
    Examples:
      cmu config alias macbook-air
      cmu config alias "office desktop"
    """
    from .config import set_alias

    console = Console()
    set_alias(name)
    console.print(f"[green]Alias set to:[/green] {name}")


@config.group(name="key", invoke_without_command=True)
@click.pass_context
def config_key(ctx):
    """Manage keys for data grouping.

    \b
    Examples:
      cmu config key add my-key "description"
      cmu config key remove my-key
      cmu config key list
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_key_list)


@config_key.command(name="add")
@click.argument("key")
@click.argument("description", default="")
def config_key_add(key: str, description: str):
    """Add or update a key with optional description.

    \b
    Examples:
      cmu config key add my-key "personal usage"
      cmu config key add team-key "team shared key"
    """
    from .config import add_key

    console = Console()
    is_new = add_key(key, description)
    if is_new:
        console.print(f"[green]Key added:[/green] {key}")
    else:
        console.print(f"[green]Key updated:[/green] {key}")
    if description:
        console.print(f"  Description: {description}")


@config_key.command(name="remove")
@click.argument("key")
def config_key_remove(key: str):
    """Remove a key.

    \b
    Example:
      cmu config key remove team-key
    """
    from .config import remove_key

    console = Console()
    removed = remove_key(key)
    if removed:
        console.print(f"[green]Key removed:[/green] {key}")
    else:
        console.print(f"[yellow]Key not found:[/yellow] {key}")


@config_key.command(name="list")
def config_key_list():
    """List all registered keys."""
    from .config import get_keys
    from rich.table import Table

    console = Console()
    keys = get_keys()

    if not keys:
        console.print("[dim]No keys configured.[/dim]")
        console.print("Run: cmu config key add <your-key>")
        return

    table = Table(box=None, padding=(0, 2))
    table.add_column("Key", style="bold")
    table.add_column("Description", style="dim")

    for k in keys:
        table.add_row(k["key"], k.get("description", ""))

    console.print()
    console.print(table)
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output.")
@click.option("--if-changed", is_flag=True,
              help="Only sync when the local usage files changed since the last sync.")
def sync(quiet: bool, if_changed: bool):
    """Sync local usage data to the central server.

    \b
    Pushes all local Claude Code usage data to the configured server.
    Data is pushed with all registered keys.

    \b
    Examples:
      cmu sync
      cmu sync --quiet
      cmu sync --if-changed      Skip the upload if nothing changed locally
    """
    from . import autosync
    from .sync_client import SyncError, sync_now

    console = Console()
    try:
        if if_changed:
            result = autosync.run(sync_now)
            if not quiet:
                console.print("[dim]Nothing changed since the last sync.[/dim]" if result == "unchanged"
                              else "[green]Synced.[/green]")
            return
        payload = sync_now()
        autosync.record_success()
    except SyncError as e:
        if not quiet:
            console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    if not quiet:
        from .config import get_server_url
        console.print(f"[green]Synced to {get_server_url()}[/green] "
                      f"({payload['hostname']}, keys: {', '.join(payload['keys'])})")


@main.command("import-claude-export", context_settings=CONTEXT_SETTINGS)
@click.argument("path", type=click.Path(exists=True))
@click.option("--alias", default=None, help="Display alias for the claude.ai pseudo-device.")
@click.option("--dry-run", is_flag=True, help="Show what would be sent without contacting the server.")
def import_claude_export(path: str, alias: str, dry_run: bool):
    """Import a claude.ai data export as the device "claude.ai".

    \b
    Request the export on claude.ai (Settings > Privacy > Export data,
    web or desktop app only), download the ZIP from the emailed link, then:
      cmu import-claude-export ~/Downloads/data-2026-10-06.zip

    \b
    The export covers every client of the account (web, desktop, Android,
    iOS). Message counts and dates are exact; tokens are estimated from
    text length (about 4 characters per token) under the model name
    "claude.ai (estimated)". Each account becomes its own device,
    claude.ai-<id>. An export is a snapshot, so each import replaces the
    previous one on the server: the most recent import wins.
    """
    from rich.panel import Panel
    from rich.table import Table
    from .claude_export import ExportError, describe, load_export, summarize_export
    from .sync_client import SyncError, post_payload, require_server_config

    console = Console()
    try:
        export = load_export(path)
    except ExportError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)

    server_url, key_values = None, []
    if not dry_run:
        try:
            server_url, key_entries = require_server_config()
        except SyncError as e:
            console.print(f"[red]{e}[/red]")
            raise SystemExit(1)
        key_values = [k["key"] for k in key_entries]

    payload = summarize_export(export, key_values, alias=alias)
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", style="dim")
    table.add_column("value", style="bold cyan")
    for label, value in describe(payload):
        table.add_row(label, value)
    console.print()
    console.print(Panel(table, title=f"claude.ai export: {path}", border_style="blue"))
    if dry_run:
        console.print("[dim]Dry run: nothing sent.[/dim]")
        console.print()
        return
    try:
        post_payload(server_url, payload)
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    console.print(f"[green]Imported to {server_url}[/green] as device '{payload['hostname']}' "
                  f"(keys: {', '.join(key_values)})")
    console.print()


@main.group("autosync", context_settings=CONTEXT_SETTINGS)
def autosync_group():
    """Keep the server updated automatically.

    \b
    Installs a per-user scheduler job (a systemd user timer on Linux, a
    launchd agent on macOS) that runs `cmu autosync run` periodically.
    That uploads only when the local usage files changed since the last
    successful sync, so an idle machine never contacts the server.

    \b
    Examples:
      cmu autosync install            Every 15 minutes
      cmu autosync install --every 5
      cmu autosync status
      cmu autosync run                Sync now if anything changed
      cmu autosync uninstall
    """


@autosync_group.command("install")
@click.option("--every", default=15, type=click.IntRange(min=1), show_default=True, metavar="MINUTES",
              help="How often to check for changes.")
@click.option("--linger", is_flag=True,
              help="Linux: also run while you are logged out (loginctl enable-linger).")
def autosync_install(every: int, linger: bool):
    """Install and start the periodic sync job for this user."""
    import subprocess
    from . import autosync
    from .sync_client import SyncError, require_server_config, sync_now

    console = Console()
    try:
        require_server_config()
        lines = autosync.install(every, linger)
    except (SyncError, RuntimeError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]{' '.join(e.cmd)} failed:[/red] {(e.stderr or e.stdout or '').strip()}")
        raise SystemExit(1)
    for line in lines:
        console.print(line)
    try:
        result = autosync.run(sync_now, force=True)
        console.print(f"[green]First sync: {result}[/green]")
    except SyncError as e:
        console.print(f"[yellow]First sync failed:[/yellow] {e}")
        console.print("[dim]The job stays installed and will retry on schedule.[/dim]")


@autosync_group.command("uninstall")
def autosync_uninstall():
    """Remove the periodic sync job."""
    from . import autosync

    for line in autosync.uninstall():
        Console().print(line)


@autosync_group.command("status")
def autosync_status():
    """Show whether the job is installed and when it last synced."""
    from . import autosync

    for line in autosync.describe_status(autosync.status()):
        Console().print(line)


@autosync_group.command("run")
@click.option("--force", is_flag=True, help="Sync even if nothing changed.")
@click.option("--quiet", "-q", is_flag=True, help="Only print when something was synced.")
def autosync_run(force: bool, quiet: bool):
    """Sync now if the local usage files changed (what the scheduler runs)."""
    from . import autosync
    from .sync_client import SyncError, sync_now

    console = Console()
    try:
        result = autosync.run(sync_now, force=force)
    except SyncError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    if result == "synced":
        console.print(f"[green]Synced at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/green]")
    elif not quiet:
        console.print("[dim]Nothing changed since the last sync.[/dim]")


@main.group("remote", context_settings=CONTEXT_SETTINGS)
def remote_group():
    """Install cmu on other machines over SSH.

    \b
    Examples:
      cmu remote install alice@laptop                    Install, configure, schedule
      cmu remote install laptop --alias laptop --every 5
      cmu remote install laptop --from git+https://github.com/you/claude-multi-usage.git
      cmu remote uninstall laptop

    \b
    The remote gets a virtualenv in ~/.local/share/cmu, a ~/.local/bin/cmu
    link, this machine's server URL and keys, and `cmu autosync install`.
    Needs Python 3.9+ with venv there (Debian/Ubuntu: python3-venv).
    """


@remote_group.command("install")
@click.argument("host")
@click.option("--every", default=15, type=click.IntRange(min=1), show_default=True, metavar="MINUTES",
              help="Autosync interval on the remote host.")
@click.option("--server", "server_url", default=None, metavar="URL",
              help="Sync server URL (default: this machine's).")
@click.option("--key", "key_values", multiple=True, metavar="KEY",
              help="Sync key, repeatable (default: this machine's keys).")
@click.option("--alias", default=None, help="Display alias for the remote machine.")
@click.option("--from", "source", default=None, metavar="PATH|REQUIREMENT",
              help="Install cmu from a source checkout (sent over ssh) or a pip requirement such as "
                   "git+https://... (default: this checkout when run from one, else the repository URL).")
@click.option("--python", default="python3", show_default=True, help="Python interpreter on the remote host.")
@click.option("--ssh-option", "ssh_options", multiple=True, metavar="OPT",
              help='Extra ssh option, repeatable, e.g. "-o StrictHostKeyChecking=accept-new".')
@click.option("--linger", is_flag=True,
              help="Linux remote: run the job while logged out too (loginctl enable-linger).")
def remote_install(host: str, every: int, server_url: str, key_values: tuple, alias: str,
                   source: str, python: str, ssh_options: tuple, linger: bool):
    """Install cmu and its autosync job on HOST (an ssh destination)."""
    import shlex
    import subprocess
    from urllib.parse import urlparse
    from . import remote
    from .config import get_keys, get_server_url

    console = Console()
    server_url = server_url or get_server_url()
    if not server_url:
        console.print("[red]No server URL: pass --server or run `cmu config server <url>` here first.[/red]")
        raise SystemExit(1)
    if (urlparse(server_url).hostname or "") in ("localhost", "127.0.0.1", "::1"):
        console.print(f"[yellow]Warning:[/yellow] {server_url} points at this machine; from {host} "
                      "that address is the remote itself. Pass --server with an address it can reach.")
    keys = [(k, "") for k in key_values] or [(k["key"], k.get("description", "")) for k in get_keys()]
    if not keys:
        console.print("[red]No keys: pass --key or run `cmu config key add <key>` here first.[/red]")
        raise SystemExit(1)
    opts = [o for group in ssh_options for o in shlex.split(group)]
    try:
        lines = remote.install(host, server_url, keys, every, alias=alias, source=source,
                               python=python, ssh_options=opts, linger=linger)
    except subprocess.CalledProcessError as e:
        if e.returncode == 255:
            console.print(f"[red]ssh to {host} failed (exit 255): check the host name, keys and network.[/red]")
        else:
            console.print(f"[red]Setup on {host} failed (exit {e.returncode}); see the output above.[/red]")
        raise SystemExit(1)
    except (RuntimeError, OSError) as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    for line in lines:
        console.print(f"[green]{line}[/green]")


@remote_group.command("uninstall")
@click.argument("host")
@click.option("--ssh-option", "ssh_options", multiple=True, metavar="OPT", help="Extra ssh option, repeatable.")
def remote_uninstall(host: str, ssh_options: tuple):
    """Remove cmu and its autosync job from HOST."""
    import shlex
    import subprocess
    from . import remote

    console = Console()
    opts = [o for group in ssh_options for o in shlex.split(group)]
    try:
        lines = remote.uninstall(host, ssh_options=opts)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]ssh to {host} failed (exit {e.returncode}).[/red]")
        raise SystemExit(1)
    for line in lines:
        console.print(f"[green]{line}[/green]")


@main.command(context_settings=CONTEXT_SETTINGS)
def tree():
    """Show usage as growing grass/tree ASCII art.

    \b
    Visualizes usage with a growth story:
      - Daily: grass pots (8 three-hour blocks)
      - Weekly: grass garden with fence (7 days)
      - Monthly: forest with grass and trees (days of month)
      - Yearly: ecosystem with trees and animals (12 months)

    \b
    Examples:
      cmu tree
    """
    from datetime import timedelta
    from .tree import (make_daily_grass, make_weekly_garden,
                       make_monthly_forest, make_yearly_ecosystem)
    from .parser import parse_today_hourly

    console = Console()
    data = load_usage_data()
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    costs = get_costs()
    daily_costs = costs[0] if costs else {}

    # -- Daily (8 three-hour blocks) --
    hourly = parse_today_hourly()
    blocks: list[tuple[int, float, int]] = []
    for blk in range(8):
        start_h = blk * 3
        blk_tokens = sum(h.tokens for h in hourly if start_h <= h.hour < start_h + 3)
        blk_cost = sum(h.cost for h in hourly if start_h <= h.hour < start_h + 3)
        blk_msgs = sum(h.message_count for h in hourly if start_h <= h.hour < start_h + 3)
        blocks.append((blk_tokens, blk_cost, blk_msgs))

    console.print()
    console.print(make_daily_grass(blocks))
    console.print()

    # -- Weekly (Mon-Sun) --
    tok_map = {t.date: t.total_tokens for t in data.daily_model_tokens}
    today_tokens = sum(b[0] for b in blocks)
    today_cost = sum(b[1] for b in blocks)

    monday = today - timedelta(days=today.weekday())
    weekly_data = []
    for i in range(7):
        d = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        tokens = tok_map.get(d, 0)
        if d == today_str:
            tokens = today_tokens if today_tokens > 0 else tokens
        day_cost = sum(info["cost"] for info in daily_costs.get(d, {}).values())
        if d == today_str:
            day_cost = today_cost
        weekly_data.append((d, tokens, day_cost))

    console.print(make_weekly_garden(weekly_data))
    console.print()

    # -- Monthly (days of current month) --
    monthly_days = []
    for day_num in range(1, today.day + 1):
        d = today.replace(day=day_num).strftime("%Y-%m-%d")
        tokens = tok_map.get(d, 0)
        if d == today_str:
            tokens = today_tokens if today_tokens > 0 else tokens
        day_cost = sum(info["cost"] for info in daily_costs.get(d, {}).values())
        if d == today_str:
            day_cost = today_cost
        monthly_days.append((day_num, tokens, day_cost))

    console.print(make_monthly_forest(monthly_days))
    console.print()

    # -- Yearly (12 months) --
    year = today.year
    yearly_data = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m in range(1, 13):
        month_prefix = f"{year}-{m:02d}"
        month_tokens = sum(
            t.total_tokens for t in data.daily_model_tokens
            if t.date.startswith(month_prefix)
        )
        month_cost = sum(
            sum(info["cost"] for info in models.values())
            for date_str, models in daily_costs.items()
            if date_str.startswith(month_prefix)
        )
        # The current month includes today's realtime data when the cached
        # sources (stats-cache / cost cache) do not already contain it.
        if m == today.month:
            today_in_stats = any(
                t.date == today_str for t in data.daily_model_tokens
            )
            if not today_in_stats and today_tokens > 0:
                month_tokens += today_tokens
            if today_str not in daily_costs:
                month_cost += today_cost
        yearly_data.append((month_names[m - 1], month_tokens, month_cost))

    console.print(make_yearly_ecosystem(yearly_data))
    console.print()


@main.command(name="server", context_settings=CONTEXT_SETTINGS)
@click.argument("action", type=click.Choice(["start"]))
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host.")
@click.option("--port", "-p", default=8000, show_default=True, help="Bind port.")
@click.option("--db-path", default=None, metavar="PATH",
              help="SQLite database path (default: /data/server.db).")
@click.option("--max-body-bytes", default=None, type=click.IntRange(min=1), metavar="N",
              help="Reject sync payloads larger than N bytes (default: 2097152).")
def server_cmd(action: str, host: str, port: int, db_path: str, max_body_bytes: int):
    """Start the sync collection server.

    \b
    Requires server extras: pip install claude-multi-usage[server]

    \b
    The API has no authentication: run it on a private network only.

    \b
    Examples:
      cmu server start
      cmu server start --host 0.0.0.0 --port 8000
      cmu server start --db-path ./data/server.db
      cmu server start --max-body-bytes 500000
    """
    if action == "start":
        try:
            import uvicorn
        except ImportError:
            click.echo(
                "Server dependencies not installed.\n"
                "Run: pip install claude-multi-usage[server]",
                err=True,
            )
            raise SystemExit(1)

        import os
        if db_path:
            os.environ["CMU_DB_PATH"] = db_path
        if max_body_bytes:
            os.environ["CMU_MAX_BODY_BYTES"] = str(max_body_bytes)

        uvicorn.run(
            "claude_multi_usage.server.app:app",
            host=host,
            port=port,
        )


if __name__ == "__main__":
    main()
