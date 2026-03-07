"""CLI entry point for claude-multi-usage."""

from __future__ import annotations

import click

from .parser import load_usage_data, parse_today_usage
from .dashboard import render_dashboard, make_projects_table, make_models_table, Console, format_tokens
from .cost_cache import get_costs
from .pricing import format_cost


CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


def _fetch_all_usage():
    """Fetch aggregated usage data from all devices via the sync server."""
    import json
    import urllib.request
    import urllib.error
    from .config import get_server_url
    from .parser import UsageData, DailyActivity, DailyModelTokens, ModelUsage, ProjectSummary
    from datetime import datetime

    console = Console()
    server_url = get_server_url()

    if not server_url:
        console.print("[red]Server URL not configured.[/red]")
        console.print("Run: cmu config --server <url>")
        raise SystemExit(1)

    try:
        req = urllib.request.Request(f"{server_url}/api/usage")
        with urllib.request.urlopen(req, timeout=10) as resp:
            devices = json.loads(resp.read())
    except urllib.error.URLError as e:
        console.print(f"[red]Failed to fetch from server:[/red] {e}")
        raise SystemExit(1)

    if not devices:
        console.print("[dim]No device data on server.[/dim]")
        raise SystemExit(0)

    # 모든 기기 데이터를 합산
    all_activity: dict[str, DailyActivity] = {}
    all_tokens: dict[str, dict[str, int]] = {}
    all_model_usage: dict[str, list[int]] = {}  # model -> [in, out, cache_read, cache_create]
    all_projects: dict[str, list] = {}  # name -> [sessions, out, in, first, last]
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


@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="claude-multi-usage")
@click.pass_context
def main(ctx):
    """Claude Code usage dashboard across multiple devices.

    \b
    Quick start:
      cmu                        Full dashboard (last 14 days)
      cmu today                  Today's realtime usage
      cmu projects               Project breakdown with token usage
      cmu projects -n 5          Top 5 projects only
      cmu models                 Model usage breakdown
      cmu cost                   Monthly cost breakdown
      cmu dashboard -d 7         Last 7 days
      cmu dashboard -d 30        Last 30 days
      cmu dashboard --from 2026-03-01 --to 2026-03-07    Date range
      cmu dashboard --all            All devices (via sync server)

    \b
    Data source:
      Reads ~/.claude/stats-cache.json and session .jsonl files.
      No API keys or network access required.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(dashboard)


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--days", "-d", default=14, show_default=True,
              help="Number of recent days to display in the chart.")
@click.option("--from", "date_from", default=None, metavar="YYYY-MM-DD",
              help="Start date for the chart range.")
@click.option("--to", "date_to", default=None, metavar="YYYY-MM-DD",
              help="End date for the chart range.")
@click.option("--all", "show_all", is_flag=True,
              help="Show aggregated data from all devices via the sync server.")
def dashboard(days: int, date_from: str, date_to: str, show_all: bool):
    """Show the full usage dashboard.

    \b
    Examples:
      cmu dashboard                 Last 14 days (default)
      cmu dashboard -d 7            Last 7 days
      cmu dashboard -d 30           Last 30 days
      cmu dashboard -d 90           Last 3 months
      cmu dashboard --from 2026-02-01                     From date to now
      cmu dashboard --from 2026-02-01 --to 2026-02-28     Specific range
      cmu dashboard --to 2026-02-28                       14 days ending at date
      cmu dashboard --all                                 All devices (via server)

    \b
    Includes: summary, model usage, daily token chart,
    hourly heatmap, and top projects.
    """
    if show_all:
        data = _fetch_all_usage()
    else:
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
    from datetime import datetime
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    data = load_usage_data()
    today_str = datetime.now().strftime("%Y-%m-%d")

    # stats-cache에서 먼저 확인
    activity = next((a for a in data.daily_activity if a.date == today_str), None)
    tokens_data = next((t for t in data.daily_model_tokens if t.date == today_str), None)

    # 없으면 세션 파일에서 실시간 계산
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

    console.print()
    console.print(Panel(table, title=f"Today - {data.hostname}", border_style="blue"))
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
    Uses Anthropic API pricing per model:
      opus:   $15/M input, $75/M output
      sonnet: $3/M input, $15/M output
      haiku:  $0.80/M input, $4/M output
    """
    from collections import defaultdict
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    daily_costs, total_cost, today_cost = get_costs()

    # 월별 집계
    monthly = defaultdict(lambda: defaultdict(float))
    for date_str, models in sorted(daily_costs.items()):
        month = date_str[:7]  # YYYY-MM
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
@click.option("--server", "server_url", default=None, metavar="URL",
              help="Set the sync server URL.")
@click.option("--show", is_flag=True, help="Show current configuration.")
def config(server_url: str, show: bool):
    """Configure claude-multi-usage settings.

    \b
    Examples:
      cmu config --server https://your-server.com
      cmu config --show
    """
    from .config import get_server_url, set_server_url, load_config

    console = Console()

    if server_url:
        set_server_url(server_url)
        console.print(f"[green]Server URL set to:[/green] {server_url}")
        return

    cfg = load_config()
    console.print()
    console.print("[bold]Current configuration:[/bold]")
    console.print(f"  server_url: {cfg.get('server_url') or '[dim]not set[/dim]'}")
    console.print()


@main.command(context_settings=CONTEXT_SETTINGS)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output.")
def sync(quiet: bool):
    """Sync local usage data to the central server.

    \b
    Pushes all local Claude Code usage data to the configured server.
    Set the server URL first with: cmu config --server <url>

    \b
    Examples:
      cmu sync
      cmu sync --quiet
    """
    import json
    import urllib.request
    import urllib.error
    from datetime import datetime
    from .config import get_server_url

    console = Console()
    server_url = get_server_url()

    if not server_url:
        if not quiet:
            console.print("[red]Server URL not configured.[/red]")
            console.print("Run: cmu config --server <url>")
        raise SystemExit(1)

    data = load_usage_data()

    payload = {
        "hostname": data.hostname,
        "synced_at": datetime.now().isoformat(),
        "daily_activity": [
            {"date": a.date, "message_count": a.message_count,
             "session_count": a.session_count, "tool_call_count": a.tool_call_count}
            for a in data.daily_activity
        ],
        "daily_model_tokens": [
            {"date": t.date, "tokens_by_model": t.tokens_by_model}
            for t in data.daily_model_tokens
        ],
        "model_usage": [
            {"model": m.model, "input_tokens": m.input_tokens,
             "output_tokens": m.output_tokens, "cache_read_tokens": m.cache_read_tokens,
             "cache_creation_tokens": m.cache_creation_tokens}
            for m in data.model_usage
        ],
        "projects": [
            {"name": p.name, "session_count": p.session_count,
             "output_tokens": p.output_tokens, "input_tokens": p.input_tokens,
             "first_seen": p.first_seen.isoformat() if p.first_seen else None,
             "last_seen": p.last_seen.isoformat() if p.last_seen else None}
            for p in data.projects
        ],
        "hour_counts": {str(k): v for k, v in data.hour_counts.items()},
        "total_sessions": data.total_sessions,
        "total_messages": data.total_messages,
        "first_session_date": data.first_session_date,
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url}/api/sync",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
            if not quiet:
                console.print(f"[green]Synced to {server_url}[/green] ({data.hostname})")
    except urllib.error.URLError as e:
        if not quiet:
            console.print(f"[red]Sync failed:[/red] {e}")
        raise SystemExit(1)


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
