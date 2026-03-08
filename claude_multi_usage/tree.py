"""Grass-growing usage visualization with ASCII art.

4 views following a growth story:
  grass pots (daily) -> grass garden (weekly) -> forest (monthly) -> ecosystem (yearly)
"""

from __future__ import annotations

from datetime import datetime

from rich.panel import Panel
from rich.text import Text

from .dashboard import format_tokens
from .pricing import format_cost


# -- Helpers ------------------------------------------------------------------

def _rel_level(value: int, max_val: int, n_levels: int) -> int:
    """Return 0..n_levels based on relative ratio to max_val."""
    if max_val <= 0 or value <= 0:
        return 0
    return min(int(value / max_val * (n_levels - 1)) + 1, n_levels)


# -- Daily Grass Pots --------------------------------------------------------
# 8 three-hour blocks, each a pot with grass growing upward.

_DW = 6  # column width

_DGRASS = ["", "|", "||", "|||", "|||"]
_DHEIGHT = [0, 1, 2, 3, 4]


def make_daily_grass(blocks: list[tuple[int, float, int]]) -> Panel:
    """Render 8 three-hour blocks as grass pots.

    blocks: [(tokens, cost, msgs)] x 8, one per 3h interval (00-02 .. 21-23).
    """
    MH = 4
    toks = [b[0] for b in blocks]
    mx = max(toks) if toks else 0
    lvls = [_rel_level(t, mx, MH) for t in toks]
    now_blk = datetime.now().hour // 3

    text = Text()
    text.append("\n")

    # grass rows (top to bottom)
    for row in range(MH, 0, -1):
        line = Text()
        line.append("  ")
        for i in range(8):
            if _DHEIGHT[lvls[i]] >= row:
                g = _DGRASS[lvls[i]]
                st = "bold green" if lvls[i] == MH else "green"
                line.append(f"{g:^{_DW}}", style=st)
            else:
                line.append(" " * _DW)
        text.append_text(line)
        text.append("\n")

    # pot row
    line = Text()
    line.append("  ")
    for i in range(8):
        st = "yellow" if toks[i] > 0 else "dim"
        line.append("[___]".center(_DW), style=st)
    text.append_text(line)
    text.append("\n\n")

    # hour labels
    line = Text()
    line.append("  ")
    for i in range(8):
        h = i * 3
        marker = "*" if i == now_blk else " "
        label = "%02d%s" % (h, marker)
        line.append(label.center(_DW), style="dim")
    text.append_text(line)
    text.append("\n")

    # token labels
    line = Text()
    line.append("  ")
    for i in range(8):
        if toks[i] > 0:
            line.append(format_tokens(toks[i]).center(_DW), style="cyan")
        else:
            line.append("\u00b7".center(_DW), style="dim")
    text.append_text(line)
    text.append("\n")

    # summary
    tot = sum(toks)
    cost = sum(b[1] for b in blocks)
    active = sum(1 for t in toks if t > 0)
    text.append("\n")
    s = "  %s tokens" % format_tokens(tot)
    if cost > 0:
        s += "  %s" % format_cost(cost)
    s += "  |  %d active blocks" % active
    text.append(s + "\n", style="bold cyan")

    today_str = datetime.now().strftime("%Y-%m-%d")
    return Panel(text, title="Today (%s)" % today_str,
                 border_style="green", width=58, padding=(0, 1))


# -- Weekly Grass Garden ------------------------------------------------------
# 7 days inside a fence, grass bars of varying height.

_WW = 9  # column width per day

_WGRASS = ["", "||", "||", "|||", "|||", "||||", "||||", "||||"]
_WHEIGHT = [0, 1, 2, 3, 4, 5, 6, 7]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def make_weekly_garden(daily: list[tuple[str, int, float]]) -> Panel:
    """Render weekly grass garden with fence.

    daily: [(date_str, tokens, cost)] x 7, Mon-Sun.
    """
    MH = 7
    INNER = _WW * 7
    toks = [d[1] for d in daily]
    mx = max(toks) if toks else 0
    lvls = [_rel_level(t, mx, MH) for t in toks]
    today_str = datetime.now().strftime("%Y-%m-%d")

    text = Text()
    text.append("\n")

    # top fence
    fence_top = "  |" + "=" * INNER + "|"
    text.append(fence_top + "\n", style="yellow")

    # grass rows
    for row in range(MH, 0, -1):
        line = Text()
        line.append("  |", style="yellow")
        for i in range(7):
            if _WHEIGHT[lvls[i]] >= row:
                g = _WGRASS[lvls[i]]
                st = "bold green" if lvls[i] >= 6 else "green"
                line.append(g.center(_WW), style=st)
            else:
                line.append(" " * _WW)
        line.append("|", style="yellow")
        text.append_text(line)
        text.append("\n")

    # ground row inside fence
    line = Text()
    line.append("  |", style="yellow")
    for i in range(7):
        if toks[i] > 0:
            dot_g = "." + _WGRASS[min(lvls[i], 2)] + "."
            line.append(dot_g.center(_WW), style="dim green")
        else:
            line.append(".".center(_WW), style="dim")
    line.append("|", style="yellow")
    text.append_text(line)
    text.append("\n")

    # bottom fence
    fence_bot = "  |" + "=" * INNER + "|"
    text.append(fence_bot + "\n", style="yellow")

    # day labels
    line = Text()
    line.append("   ")
    for i, d in enumerate(_DAYS):
        marker = "*" if daily[i][0] == today_str else " "
        line.append((d + marker).center(_WW), style="dim")
    text.append_text(line)
    text.append("\n")

    # token labels
    line = Text()
    line.append("   ")
    for i in range(7):
        if toks[i] > 0:
            line.append(format_tokens(toks[i]).center(_WW), style="cyan")
        else:
            line.append("\u00b7".center(_WW), style="dim")
    text.append_text(line)
    text.append("\n")

    # summary
    tot = sum(toks)
    cost = sum(d[2] for d in daily)
    active = sum(1 for t in toks if t > 0)
    text.append("\n")
    s = "  Total: %s tokens" % format_tokens(tot)
    if cost > 0:
        s += "  %s" % format_cost(cost)
    s += "  |  %d/7 days active" % active
    text.append(s + "\n", style="bold cyan")

    start = daily[0][0]
    end = daily[-1][0]
    return Panel(text, title="This Week (%s ~ %s)" % (start[5:], end[5:]),
                 border_style="cyan", padding=(0, 1))


# -- Monthly Forest -----------------------------------------------------------
# Each day of the month: grass for low usage, trees for high usage.

_MW = 3  # column width per day
_MMAX = 5  # max level


def _monthly_col(level: int) -> list[str]:
    """Return column art (list of strings, bottom to top) for a day.

    Each string is _MW chars wide.
    """
    # Use .center() to avoid f-string backslash issues
    pipe = "|".center(_MW)
    dpipe = "||".center(_MW)
    tree_top = "/\\".center(_MW)

    if level == 0:
        return []
    elif level == 1:
        return [pipe]
    elif level == 2:
        return [pipe, pipe]
    elif level == 3:
        return [dpipe, dpipe, dpipe]
    elif level == 4:
        return [dpipe, dpipe, tree_top]
    else:
        return [dpipe, dpipe, dpipe, tree_top]


def _monthly_style(level: int, row_from_bottom: int) -> str:
    """Style for a monthly column cell."""
    if level >= 4:
        total = len(_monthly_col(level))
        if row_from_bottom >= total - 1:
            return "bold green"  # canopy
        return "yellow"  # trunk
    return "green"


def make_monthly_forest(days: list[tuple[int, int, float]]) -> Panel:
    """Render monthly forest view.

    days: [(day_num, tokens, cost)] for each day of the month (up to today).
    """
    n = len(days)
    toks = [d[1] for d in days]
    mx = max(toks) if toks else 0
    lvls = [_rel_level(t, mx, _MMAX) for t in toks]
    today_day = datetime.now().day

    # precompute column art
    cols = [_monthly_col(lv) for lv in lvls]
    max_h = max((len(c) for c in cols), default=0)

    text = Text()
    text.append("\n")

    # art rows (top to bottom)
    for row_top in range(max_h):
        row_bottom = max_h - 1 - row_top
        line = Text()
        line.append("  ")
        for i in range(n):
            col = cols[i]
            if row_bottom < len(col):
                cell = col[row_bottom]
                st = _monthly_style(lvls[i], row_bottom)
                line.append(cell, style=st)
            else:
                line.append(" " * _MW)
        text.append_text(line)
        text.append("\n")

    # ground line
    line = Text()
    line.append("  ")
    ground = "~^~" * (n + 1)
    line.append(ground[:n * _MW], style="dim yellow")
    text.append_text(line)
    text.append("\n")

    # day labels (show 1st, every 5th, last, today)
    line = Text()
    line.append("  ")
    for i, (day, _, _) in enumerate(days):
        show = (day == 1 or day % 5 == 0 or day == n or day == today_day)
        if show:
            marker = "*" if day == today_day else ""
            label = str(day) + marker
            line.append(label.ljust(_MW), style="dim")
        else:
            line.append(" " * _MW)
    text.append_text(line)
    text.append("\n")

    # summary
    tot = sum(toks)
    cost = sum(d[2] for d in days)
    active = sum(1 for t in toks if t > 0)
    text.append("\n")
    s = "  Total: %s tokens" % format_tokens(tot)
    if cost > 0:
        s += "  %s" % format_cost(cost)
    s += "  |  %d/%d days active" % (active, today_day)
    text.append(s + "\n", style="bold cyan")

    month_name = datetime.now().strftime("%B %Y")
    return Panel(text, title="This Month (%s)" % month_name,
                 border_style="magenta", padding=(0, 1))


# -- Yearly Ecosystem --------------------------------------------------------
# 12 months with trees, decorative animals at higher ecosystem levels.

_YW = 6  # column width per month (12*6+margins fits in 80-col terminal)
_YMAX = 4  # max tree level per month
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_ECO_LABELS = ["barren", "sprouting", "growing", "young forest", "forest", "ecosystem"]


def _eco_level(active_months: int) -> int:
    """0-5 ecosystem level based on active month count."""
    if active_months == 0:
        return 0
    if active_months <= 2:
        return 1
    if active_months <= 4:
        return 2
    if active_months <= 6:
        return 3
    if active_months <= 9:
        return 4
    return 5


def _yearly_col(level: int) -> list[str]:
    """Column art for a month (bottom to top), each _YW chars wide."""
    pipe = "|".center(_YW)
    dpipe = "||".center(_YW)
    canopy_sm = "/\\".center(_YW)
    canopy_lg = "/ \\".center(_YW)

    if level == 0:
        return []
    elif level == 1:
        return [pipe]
    elif level == 2:
        return [dpipe, canopy_sm]
    elif level == 3:
        return [dpipe, dpipe, canopy_lg, canopy_sm]
    else:
        return [dpipe, dpipe, dpipe, canopy_lg, canopy_sm]


def _yearly_style(level: int, row_from_bottom: int) -> str:
    """Style for yearly column cell."""
    total = len(_yearly_col(level))
    if total == 0:
        return "dim"
    # top 2 rows = canopy (green), lower rows = trunk (yellow)
    if row_from_bottom >= total - 2 and level >= 2:
        return "bold green"
    if level <= 1:
        return "green"
    return "yellow"


def make_yearly_ecosystem(monthly: list[tuple[str, int, float]]) -> Panel:
    """Render yearly ecosystem view.

    monthly: [(month_label, tokens, cost)] x 12, Jan-Dec.
    """
    toks = [m[1] for m in monthly]
    mx = max(toks) if toks else 0
    active = sum(1 for t in toks if t > 0)
    eco = _eco_level(active)
    lvls = [_rel_level(t, mx, _YMAX) for t in toks]
    cur_month = datetime.now().month

    # precompute columns
    cols = [_yearly_col(lv) for lv in lvls]
    max_h = max((len(c) for c in cols), default=0)

    # animal strings (pre-computed to avoid backslash in f-strings)
    rabbit = "(\\(\\"
    deer = "/\\_/\\"

    text = Text()
    text.append("\n")

    # decoration row (birds, butterflies for eco >= 4)
    if eco >= 4:
        line = Text()
        line.append("  ")
        for i in range(12):
            if lvls[i] >= 3 and i % 3 == 0:
                line.append("*".center(_YW), style="bold magenta")
            elif lvls[i] >= 2 and i % 4 == 1:
                line.append(",".center(_YW), style="dim cyan")
            else:
                line.append(" " * _YW)
        text.append_text(line)
        text.append("\n")

    # tree/grass rows (top to bottom)
    for row_top in range(max_h):
        row_bottom = max_h - 1 - row_top
        line = Text()
        line.append("  ")
        for i in range(12):
            col = cols[i]
            if row_bottom < len(col):
                st = _yearly_style(lvls[i], row_bottom)
                line.append(col[row_bottom], style=st)
            else:
                line.append(" " * _YW)
        text.append_text(line)
        text.append("\n")

    # animal/ground row (eco >= 4)
    if eco >= 4:
        line = Text()
        line.append("  ")
        placed = set()
        for i in range(12):
            if eco >= 5 and i == 2 and toks[i] > 0 and "rabbit" not in placed:
                line.append(rabbit.center(_YW), style="dim yellow")
                placed.add("rabbit")
            elif eco >= 5 and i == 8 and toks[i] > 0 and "deer" not in placed:
                line.append(deer.center(_YW), style="dim yellow")
                placed.add("deer")
            elif eco >= 4 and i == 5 and toks[i] > 0 and "rabbit" not in placed:
                line.append(rabbit.center(_YW), style="dim yellow")
                placed.add("rabbit")
            elif toks[i] > 0:
                line.append(".|..".center(_YW), style="dim green")
            else:
                line.append(".".center(_YW), style="dim")
        text.append_text(line)
        text.append("\n")

    # ground line
    line = Text()
    line.append("  ")
    total_w = 12 * _YW
    ground = "~^~" * (total_w // 3 + 1)
    line.append(ground[:total_w], style="dim yellow")
    text.append_text(line)
    text.append("\n")

    # month labels
    line = Text()
    line.append("  ")
    for i, m in enumerate(_MONTHS):
        marker = "*" if i + 1 == cur_month else " "
        line.append((m + marker).center(_YW), style="dim")
    text.append_text(line)
    text.append("\n")

    # token labels
    line = Text()
    line.append("  ")
    for i in range(12):
        if toks[i] > 0:
            line.append(format_tokens(toks[i]).center(_YW), style="cyan")
        else:
            line.append("\u00b7".center(_YW), style="dim")
    text.append_text(line)
    text.append("\n")

    # summary
    tot = sum(toks)
    cost = sum(m[2] for m in monthly)
    eco_label = _ECO_LABELS[eco]
    text.append("\n")
    s = "  Ecosystem: %s  |  %s tokens" % (eco_label, format_tokens(tot))
    if cost > 0:
        s += "  %s" % format_cost(cost)
    s += "  |  %d/12 months active" % active
    text.append(s + "\n", style="bold cyan")

    year = datetime.now().year
    return Panel(text, title="This Year (%d)" % year,
                 border_style="red", width=82, padding=(0, 1))
