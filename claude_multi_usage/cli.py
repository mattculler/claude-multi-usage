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

    # 특정 key가 지정되면 해당 key만, 아니면 등록된 모든 key로 조회
    query_keys = [key] if key else [k["key"] for k in keys]

    all_devices: dict[str, dict] = {}  # hostname -> device data (중복 제거)
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

    \b
    Data source:
      Reads ~/.claude/stats-cache.json and session .jsonl files.
      No API keys required. Network is used only to fetch model pricing
      from LiteLLM's GitHub repo (cached 24h) and for opt-in `cmu sync`.
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
    Parses session .jsonl files directly, so it works even
    before stats-cache.json is updated by Claude Code.

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

    # 오늘 비용 계산
    from .cost_cache import _parse_sessions_for_date_range
    from .pricing import get_pricing
    if get_pricing() is not None:
        today_daily = _parse_sessions_for_date_range(today_str, today_str)
        today_cost = sum(
            d["cost"] for models in today_daily.values() for d in models.values()
        )
        table.add_row("Cost", f"[bold yellow]{format_cost(today_cost)}[/bold yellow]")

    console.print()
    console.print(Panel(table, title=f"Today - {data.hostname}", border_style="blue"))
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
def hourly():
    """Show today's usage broken down by hour.

    \b
    Parses session .jsonl files to show per-hour token usage,
    message counts, and session counts for today.

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
    Parses all session .jsonl files to calculate per-project
    token usage. Shows sessions, output tokens, and last used date.
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
def cost():
    """Show monthly cost breakdown.

    \b
    Calculates accurate costs by parsing session files with
    incremental caching. Past days are cached (fixed),
    today is calculated in realtime.

    \b
    Pricing is fetched from LiteLLM's pricing DB and cached locally.
    Falls back to last cached pricing if network is unavailable.
    """
    from collections import defaultdict
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
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
def sync(quiet: bool):
    """Sync local usage data to the central server.

    \b
    Pushes all local Claude Code usage data to the configured server.
    Data is pushed with all registered keys.

    \b
    Examples:
      cmu sync
      cmu sync --quiet
    """
    import json
    import urllib.request
    import urllib.error
    from .config import get_server_url, get_keys, get_alias

    console = Console()
    server_url = get_server_url()

    if not server_url:
        if not quiet:
            console.print("[red]Server URL not configured.[/red]")
            console.print("Run: cmu config server <url>")
        raise SystemExit(1)

    keys = get_keys()
    if not keys:
        if not quiet:
            console.print("[red]No keys configured.[/red]")
            console.print("Run: cmu config key add <your-key>")
        raise SystemExit(1)

    data = load_usage_data()
    key_values = [k["key"] for k in keys]

    from .parser import parse_today_hourly
    today_hourly = parse_today_hourly()

    payload = {
        "hostname": data.hostname,
        "alias": get_alias(),
        "keys": key_values,
        "synced_at": datetime.now().isoformat(),
        # Cumulative figures from stats-cache.json. They are a few hundred
        # bytes and the diff view's Summary / Model Usage panels need them.
        "total_sessions": data.total_sessions,
        "total_messages": data.total_messages,
        "first_session_date": data.first_session_date,
        "model_usage": [
            {"model": m.model, "input_tokens": m.input_tokens,
             "output_tokens": m.output_tokens, "cache_read_tokens": m.cache_read_tokens,
             "cache_creation_tokens": m.cache_creation_tokens}
            for m in data.model_usage
        ],
        "hour_counts": {str(k): v for k, v in data.hour_counts.items()},
        # Which day today_hourly describes, so stale data is labelled correctly
        "today_hourly_date": datetime.now().strftime("%Y-%m-%d"),
        "today_hourly": [
            {"hour": h.hour, "message_count": h.message_count,
             "session_count": h.session_count, "tokens": h.tokens}
            for h in today_hourly
        ],
        "daily_activity": [
            {"date": a.date, "message_count": a.message_count,
             "session_count": a.session_count, "tool_call_count": a.tool_call_count}
            for a in data.daily_activity
        ],
        "daily_model_tokens": [
            {"date": t.date, "tokens_by_model": t.tokens_by_model}
            for t in data.daily_model_tokens
        ],
        "projects": [
            {"name": p.name, "session_count": p.session_count,
             "output_tokens": p.output_tokens, "input_tokens": p.input_tokens,
             "first_seen": p.first_seen.isoformat() if p.first_seen else None,
             "last_seen": p.last_seen.isoformat() if p.last_seen else None}
            for p in data.projects
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/api/sync",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "cmu"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
            if not quiet:
                keys_str = ", ".join(key_values)
                console.print(f"[green]Synced to {server_url}[/green] ({data.hostname}, keys: {keys_str})")
    except urllib.error.URLError as e:
        if not quiet:
            console.print(f"[red]Sync failed:[/red] {e}")
        raise SystemExit(1)


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
def server_cmd(action: str, host: str, port: int, db_path: str):
    """Start the sync collection server.

    \b
    Requires server extras: pip install claude-multi-usage[server]

    \b
    Examples:
      cmu server start
      cmu server start --host 0.0.0.0 --port 8000
      cmu server start --db-path ./data/server.db
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

        uvicorn.run(
            "claude_multi_usage.server.app:app",
            host=host,
            port=port,
        )


if __name__ == "__main__":
    main()
