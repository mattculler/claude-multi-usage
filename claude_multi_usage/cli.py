"""CLI entry point for claude-multi-usage."""

from __future__ import annotations

import click

from .parser import load_usage_data, parse_today_usage
from .dashboard import render_dashboard, make_projects_table, make_models_table, Console, format_tokens


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Claude Code usage dashboard across multiple devices."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(dashboard)


@main.command()
@click.option("--days", "-d", default=14, help="Number of days to show in chart")
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
def dashboard(days: int, date_from: str, date_to: str):
    """Show the full usage dashboard."""
    data = load_usage_data()
    render_dashboard(data, days=days, date_from=date_from, date_to=date_to)


@main.command()
def today():
    """Show today's usage (realtime, parsed from session files)."""
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


@main.command()
def projects():
    """Show project usage breakdown."""
    console = Console()
    data = load_usage_data()
    console.print()
    console.print(make_projects_table(data, limit=20))
    console.print()


@main.command()
def models():
    """Show model usage breakdown."""
    console = Console()
    data = load_usage_data()
    console.print()
    console.print(make_models_table(data))
    console.print()


if __name__ == "__main__":
    main()
