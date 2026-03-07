# claude-multi-usage (cmu)

CLI dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage tracking across multiple devices.

Parses local `~/.claude` data and displays usage stats in your terminal — sessions, tokens, projects, models, and more.

## Install

```bash
pipx install claude-multi-usage
```

> Requires [pipx](https://pipx.pypa.io/). Install with `brew install pipx` (macOS) or `pip install pipx`.

## Usage

```bash
cmu                    # Full dashboard
cmu today              # Today's usage
cmu projects           # Usage by project
cmu models             # Usage by model
cmu dashboard -d 30    # Last 30 days
```

`claude-multi-usage` also works as a command alias.

## Dashboard Preview

```
── Claude Usage Dashboard  ──  my-macbook.local  ──  2026-03-07 ──

╭──────────── Summary ────────────╮  ╭────────── Model Usage ──────────╮
│  Hostname       my-macbook      │  │  Model           Output  Cache  │
│  Total Sessions 132             │  │  claude-opus-4-6  780K   519M   │
│  Total Messages 35,330          │  │  claude-sonnet    867K   424M   │
│  Output Tokens  1.6M            │  ╰─────────────────────────────────╯
╰─────────────────────────────────╯

╭──────────────── Daily Tokens (last 14 days) ─────────────────╮
│  02-23  ████░░░░░░░░░░░░░░░░   60.3K  (1233 msgs, 9 sess)   │
│  02-24  █░░░░░░░░░░░░░░░░░░░   24.8K  (909 msgs, 4 sess)    │
│  03-03  ███████░░░░░░░░░░░░░   96.6K  (633 msgs, 4 sess)    │
╰──────────────────────────────────────────────────────────────╯

╭──────────────── Hourly Sessions (all time) ──────────────────╮
│  00 01 02 .. 09 10 .. 16 17 18 19 20 21 22 23               │
│  ▒▒ ░░ ░░    ░░ ▒▒    ▓▓ ▒▒ ░░ ▒▒ ▓▓ ▓▓ ██ ▓▓               │
╰──────────────────────────────────────────────────────────────╯

╭──────────────── Top Projects ────────────────────────────────╮
│  1. apr-backend-assignment   45 sessions   2026-02-10       │
│  2. wemade-assignment        23 sessions   2026-03-04       │
│  3. url-jarvis               17 sessions   2026-03-07       │
╰──────────────────────────────────────────────────────────────╯
```

## How It Works

Reads local Claude Code data from `~/.claude/`:
- `stats-cache.json` — daily activity, model tokens, hourly counts
- `projects/` — session files per project

No API keys or network access required. All data stays local.

## Roadmap

- [ ] Multi-device sync via central server ([#2](https://github.com/hunknownn/claude-multi-usage/issues/2))
- [ ] Web dashboard ([#3](https://github.com/hunknownn/claude-multi-usage/issues/3))
- [ ] Homebrew support

## License

MIT
