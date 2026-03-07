"""Rich-based terminal dashboard for Claude Code usage."""

from __future__ import annotations

from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns

from .parser import UsageData
from .pricing import calculate_model_cost, format_cost


def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def make_summary_panel(data: UsageData) -> Panel:
    """Overall summary stats."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("label", style="dim")
    table.add_column("value", style="bold cyan")

    table.add_row("Hostname", data.hostname)
    table.add_row("Total Sessions", str(data.total_sessions))
    table.add_row("Total Messages", f"{data.total_messages:,}")

    if data.first_session_date:
        first = data.first_session_date[:10]
        table.add_row("First Session", first)

    total_output = sum(m.output_tokens for m in data.model_usage)
    table.add_row("Total Output Tokens", format_tokens(total_output))

    total_cost = sum(
        calculate_model_cost(m.model, m.input_tokens, m.output_tokens,
                             m.cache_read_tokens, m.cache_creation_tokens)
        for m in data.model_usage
    )
    table.add_row("Estimated Cost", f"[bold yellow]{format_cost(total_cost)}[/bold yellow]")

    return Panel(table, title="Summary", border_style="blue")


def make_daily_chart(data: UsageData, days: int = 14,
                     date_from: str = None, date_to: str = None) -> Panel:
    """Daily usage bar chart for recent days."""
    act_map = {a.date: a for a in data.daily_activity}
    token_map = {dt.date: dt.total_tokens for dt in data.daily_model_tokens}

    # 날짜 범위 결정
    if date_from and date_to:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        title = f"Daily Tokens ({date_from} ~ {date_to})"
    elif date_from:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.now()
        title = f"Daily Tokens ({date_from} ~ now)"
    elif date_to:
        end = datetime.strptime(date_to, "%Y-%m-%d")
        start = end - timedelta(days=days - 1)
        title = f"Daily Tokens (~ {date_to})"
    else:
        if not data.daily_activity:
            return Panel("No data", title="Daily Usage")
        end = datetime.strptime(data.daily_activity[-1].date, "%Y-%m-%d")
        start = end - timedelta(days=days - 1)
        title = f"Daily Tokens (last {days} days)"

    # 날짜 범위를 채워서 빈 날도 표시
    all_dates = []
    max_tokens = 1
    current = start
    while current <= end:
        d = current.strftime("%Y-%m-%d")
        a = act_map.get(d)
        tokens = token_map.get(d, 0)
        msgs = a.message_count if a else 0
        sessions = a.session_count if a else 0
        all_dates.append((d, tokens, msgs, sessions))
        if tokens > max_tokens:
            max_tokens = tokens
        current += timedelta(days=1)

    bar_width = 30
    lines = []
    for date_str, tokens, msgs, sessions in all_dates:
        short_date = date_str[5:]  # MM-DD
        filled = int((tokens / max_tokens) * bar_width) if max_tokens > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

        if tokens > 0:
            line = f"  {short_date}  {bar}  {format_tokens(tokens):>6}  ({msgs} msgs, {sessions} sess)"
        else:
            empty_bar = "\u2591" * bar_width
            line = f"  {short_date}  {empty_bar}"
        lines.append(line)

    return Panel("\n".join(lines), title=title, border_style="green")


def make_hourly_heatmap(data: UsageData) -> Panel:
    """24-hour session heatmap."""
    if not data.hour_counts:
        return Panel("No data", title="Hourly Activity")

    max_count = max(data.hour_counts.values()) if data.hour_counts else 1
    blocks = "  ░░▒▒▓▓██"
    styles = ["dim", "green", "yellow", "red", "bold red"]

    table = Table(box=None, padding=(0, 0), show_header=True, header_style="dim")
    for h in range(24):
        table.add_column(f"{h:02d}", justify="center", width=4)

    cells = []
    for h in range(24):
        count = data.hour_counts.get(h, 0)
        if count == 0:
            idx = 0
        else:
            idx = min(int((count / max_count) * 4) + 1, 4)
        block = blocks[idx * 2:idx * 2 + 2]
        style = styles[idx]
        cells.append(f"[{style}]{block}[/{style}]")

    table.add_row(*cells)

    # 수치 행
    count_cells = []
    for h in range(24):
        count = data.hour_counts.get(h, 0)
        count_cells.append(f"[dim]{count}[/dim]" if count > 0 else "[dim]·[/dim]")
    table.add_row(*count_cells)

    return Panel(table, title="Hourly Sessions (all time)", border_style="yellow")


def make_projects_table(data: UsageData, limit: int = 10) -> Panel:
    """Top projects by token usage."""
    table = Table(box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Project", style="bold")
    table.add_column("Sessions", justify="right", style="cyan")
    table.add_column("Output Tokens", justify="right", style="green")
    table.add_column("Last Used", style="dim")

    for i, project in enumerate(data.projects[:limit], 1):
        last = project.last_seen.strftime("%Y-%m-%d") if project.last_seen else "-"
        tokens_str = format_tokens(project.output_tokens) if project.output_tokens > 0 else "-"
        table.add_row(str(i), project.name, str(project.session_count), tokens_str, last)

    return Panel(table, title=f"Top Projects (total {len(data.projects)})", border_style="magenta")


def make_models_table(data: UsageData) -> Panel:
    """Model usage breakdown."""
    table = Table(box=None, padding=(0, 1))
    table.add_column("Model", style="bold")
    table.add_column("Output", justify="right", style="cyan")
    table.add_column("Cache Read", justify="right", style="green")
    table.add_column("Cache Create", justify="right", style="yellow")
    table.add_column("Cost", justify="right", style="bold yellow")

    for m in sorted(data.model_usage, key=lambda x: x.output_tokens, reverse=True):
        short_name = m.model.split("-202")[0] if "-202" in m.model else m.model
        cost = calculate_model_cost(
            m.model, m.input_tokens, m.output_tokens,
            m.cache_read_tokens, m.cache_creation_tokens,
        )
        table.add_row(
            short_name,
            format_tokens(m.output_tokens),
            format_tokens(m.cache_read_tokens),
            format_tokens(m.cache_creation_tokens),
            format_cost(cost),
        )

    return Panel(table, title="Model Usage", border_style="red")


def render_dashboard(data: UsageData, days: int = 14,
                     date_from: str = None, date_to: str = None) -> None:
    """Render the full dashboard."""
    console = Console()

    today = datetime.now().strftime("%Y-%m-%d")
    title = f"Claude Usage Dashboard  ──  {data.hostname}  ──  {today}"
    console.print()
    console.rule(f"[bold blue]{title}[/bold blue]")
    console.print()

    # Summary + Models side by side
    console.print(Columns([make_summary_panel(data), make_models_table(data)], equal=True))
    console.print()

    # Daily chart
    console.print(make_daily_chart(data, days=days, date_from=date_from, date_to=date_to))
    console.print()

    # Hourly heatmap
    console.print(make_hourly_heatmap(data))
    console.print()

    # Projects
    console.print(make_projects_table(data))
    console.print()
