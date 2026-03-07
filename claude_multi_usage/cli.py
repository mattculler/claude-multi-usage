"""CLI entry point for claude-multi-usage."""

from __future__ import annotations

import click

from .parser import load_usage_data, parse_today_usage
from .dashboard import render_dashboard, make_projects_table, make_models_table, Console, format_tokens


CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="claude-multi-usage")
@click.pass_context
def main(ctx):
    """Claude Code usage dashboard across multiple devices.

    \b
    Quick start:
      cmu               Show full dashboard (default)
      cmu today          Today's realtime usage
      cmu projects       Project breakdown with token usage
      cmu models         Model usage breakdown
      cmu dashboard -d 30          Last 30 days
      cmu dashboard --from 2026-03-01 --to 2026-03-07    Date range

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
def dashboard(days: int, date_from: str, date_to: str):
    """Show the full usage dashboard.

    \b
    Examples:
      cmu dashboard                          Last 14 days (default)
      cmu dashboard -d 30                    Last 30 days
      cmu dashboard --from 2026-02-01        From date to now
      cmu dashboard --from 2026-02-01 --to 2026-02-28    Specific range

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


if __name__ == "__main__":
    main()
