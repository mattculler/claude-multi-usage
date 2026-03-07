"""Rich-based terminal dashboard for Claude Code usage."""

from __future__ import annotations

from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns
from rich.rule import Rule

from .parser import UsageData, HourlyUsage
from .pricing import format_cost
from .cost_cache import get_costs


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

    costs = get_costs()
    if costs is not None:
        _, total_cost, _ = costs
        table.add_row("Estimated Cost", f"[bold yellow]{format_cost(total_cost)}[/bold yellow]")
    else:
        table.add_row("Estimated Cost", "[dim]pricing data unavailable[/dim]")

    return Panel(table, title="Summary", border_style="blue")


def make_daily_chart(data: UsageData, days: int = 14,
                     date_from: str = None, date_to: str = None,
                     daily_costs: dict = None) -> Panel:
    """Daily usage bar chart for recent days."""
    act_map = {a.date: a for a in data.daily_activity}
    token_map = {dt.date: dt.total_tokens for dt in data.daily_model_tokens}

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

    # 일별 비용 계산
    cost_map: dict[str, float] = {}
    if daily_costs:
        for date_str, models in daily_costs.items():
            cost_map[date_str] = sum(info["cost"] for info in models.values())

    bar_width = 30
    lines = []
    for date_str, tokens, msgs, sessions in all_dates:
        short_date = date_str[5:]  # MM-DD
        filled = int((tokens / max_tokens) * bar_width) if max_tokens > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

        if tokens > 0:
            day_cost = cost_map.get(date_str, 0.0)
            cost_str = f"  {format_cost(day_cost)}" if day_cost > 0 else ""
            line = f"  {short_date}  {bar}  {format_tokens(tokens):>6}{cost_str}  ({msgs} msgs, {sessions} sess)"
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

    count_cells = []
    for h in range(24):
        count = data.hour_counts.get(h, 0)
        count_cells.append(f"[dim]{count}[/dim]" if count > 0 else "[dim]·[/dim]")
    table.add_row(*count_cells)

    return Panel(table, title="Hourly Sessions (all time)", border_style="yellow")


def make_today_hourly_chart(hourly: list[HourlyUsage]) -> Panel:
    """Today's 24-hour usage bar chart."""
    now_hour = datetime.now().hour
    max_tokens = max((h.tokens for h in hourly), default=1) or 1
    bar_width = 25
    lines = []

    for h in hourly:
        if h.hour > now_hour + 1:
            break
        marker = " *" if h.hour == now_hour else "  "
        hour_label = f"{h.hour:02d}:00"
        if h.tokens > 0:
            filled = int((h.tokens / max_tokens) * bar_width)
            bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
            cost_str = f"  {format_cost(h.cost)}" if h.cost > 0 else ""
            line = f"{marker} {hour_label}  {bar}  {format_tokens(h.tokens):>6}{cost_str}  ({h.message_count} msgs, {h.session_count} sess)"
        else:
            empty_bar = "\u2591" * bar_width
            line = f"{marker} {hour_label}  {empty_bar}"
        lines.append(line)

    total_tokens = sum(h.tokens for h in hourly)
    total_msgs = sum(h.message_count for h in hourly)
    total_cost = sum(h.cost for h in hourly)
    lines.append("")
    cost_str = f", {format_cost(total_cost)}" if total_cost > 0 else ""
    lines.append(f"   Total   {format_tokens(total_tokens)} tokens, {total_msgs} messages{cost_str}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    return Panel("\n".join(lines), title=f"Today Hourly Usage ({today_str})", border_style="cyan")


def make_projects_table(data: UsageData, limit: int = 10) -> Panel:
    """Top projects by token usage."""
    has_costs = any(p.cost > 0 for p in data.projects[:limit])

    table = Table(box=None, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Project", style="bold")
    table.add_column("Sessions", justify="right", style="cyan")
    table.add_column("Output Tokens", justify="right", style="green")
    if has_costs:
        table.add_column("Cost", justify="right", style="bold yellow")
    table.add_column("Last Used", style="dim")

    for i, project in enumerate(data.projects[:limit], 1):
        last = project.last_seen.strftime("%Y-%m-%d") if project.last_seen else "-"
        tokens_str = format_tokens(project.output_tokens) if project.output_tokens > 0 else "-"
        row = [str(i), project.name, str(project.session_count), tokens_str]
        if has_costs:
            row.append(format_cost(project.cost) if project.cost > 0 else "-")
        row.append(last)
        table.add_row(*row)

    return Panel(table, title=f"Top Projects (total {len(data.projects)})", border_style="magenta")


def make_models_table(data: UsageData) -> Panel:
    """Model usage breakdown with costs from cache."""
    costs = get_costs()
    has_costs = costs is not None

    model_costs = {}
    if has_costs:
        daily_costs, _, _ = costs
        for models in daily_costs.values():
            for model, info in models.items():
                short = model.split("-202")[0] if "-202" in model else model
                model_costs[short] = model_costs.get(short, 0.0) + info["cost"]

    table = Table(box=None, padding=(0, 1))
    table.add_column("Model", style="bold")
    table.add_column("Output", justify="right", style="cyan")
    table.add_column("Cache Read", justify="right", style="green")
    table.add_column("Cache Create", justify="right", style="yellow")
    table.add_column("Cost", justify="right", style="bold yellow")

    for m in sorted(data.model_usage, key=lambda x: x.output_tokens, reverse=True):
        short_name = m.model.split("-202")[0] if "-202" in m.model else m.model
        cost_str = format_cost(model_costs.get(short_name, 0.0)) if has_costs else "-"
        table.add_row(
            short_name,
            format_tokens(m.output_tokens),
            format_tokens(m.cache_read_tokens),
            format_tokens(m.cache_creation_tokens),
            cost_str,
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

    # Daily chart (with cost data if available)
    costs = get_costs()
    daily_costs = costs[0] if costs else None
    console.print(make_daily_chart(data, days=days, date_from=date_from, date_to=date_to, daily_costs=daily_costs))
    console.print()

    # Today hourly chart
    from .parser import parse_today_hourly
    hourly = parse_today_hourly()
    if any(h.tokens > 0 or h.message_count > 0 for h in hourly):
        console.print(make_today_hourly_chart(hourly))
        console.print()

    # Hourly heatmap
    console.print(make_hourly_heatmap(data))
    console.print()

    # Projects
    console.print(make_projects_table(data))
    console.print()


def _device_display_name(data: UsageData) -> str:
    """Return display name: alias (hostname) or just hostname."""
    if hasattr(data, "alias") and data.alias:
        return f"{data.alias} ({data.hostname})"
    return data.hostname


def render_multi_device_dashboard(
    devices_data: list[UsageData],
    days: int = 14,
    date_from: str = None,
    date_to: str = None,
) -> None:
    """Render separate dashboards per device (for diff view)."""
    console = Console()
    today = datetime.now().strftime("%Y-%m-%d")

    console.print()
    console.rule(f"[bold blue]Claude Diff Dashboard  ──  {len(devices_data)} devices  ──  {today}[/bold blue]")
    console.print()

    for i, data in enumerate(devices_data):
        if i > 0:
            console.print()

        display_name = _device_display_name(data)
        console.rule(f"[bold cyan]── {display_name} ──[/bold cyan]")
        console.print()

        # Summary + Models side by side
        console.print(Columns([make_summary_panel(data), make_models_table(data)], equal=True))
        console.print()

        # Daily chart
        console.print(make_daily_chart(data, days=days, date_from=date_from, date_to=date_to))
        console.print()

        # Today hourly chart
        if hasattr(data, 'today_hourly') and data.today_hourly and any(
            h.tokens > 0 or h.message_count > 0 for h in data.today_hourly
        ):
            console.print(make_today_hourly_chart(data.today_hourly))
            console.print()

        # Projects
        console.print(make_projects_table(data))
        console.print()
