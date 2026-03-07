# claude-multi-usage (cmu)

CLI dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage tracking across multiple devices.

Parses local `~/.claude` data and displays usage stats in your terminal — sessions, tokens, projects, models, and more. Supports multi-device sync via a central server.

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
cmu cost               # Monthly cost breakdown
cmu dashboard -d 30    # Last 30 days
cmu dashboard --all    # All devices (via sync server)
```

`claude-multi-usage` also works as a command alias.

## Multi-Device Sync

Collect usage data from multiple machines into a central server.

### Server Setup

Choose one of the following:

**Docker (recommended)**
```bash
docker run -d -p 8000:8000 -v cmu-data:/data ghcr.io/hunknownn/claude-multi-usage:latest
```

**Kubernetes**
```bash
kubectl apply -f deploy/k8s/
```

**pip**
```bash
pip install claude-multi-usage[server]
cmu server start --host 0.0.0.0 --port 8000
```

### Client Setup

```bash
# Set server URL and email (once per device)
cmu config --server https://your-server.com
cmu config --email donghun@example.com

# Sync usage data
cmu sync

# View all your devices' usage (filtered by email)
cmu dashboard --all
```

> Email is used to isolate data between users. Only devices with the same email can see each other's data. Local commands (`cmu dashboard`, `cmu today`, `cmu cost`, etc.) work without email.

### Auto Sync

Add to `~/.zshrc` to sync automatically when using Claude:

```bash
cc() {
    cmu sync --quiet &>/dev/null &
    command claude "$@"
    cmu sync --quiet &>/dev/null &
}
```

## Dashboard Preview

```
── Claude Usage Dashboard  ──  my-macbook.local  ──  2026-03-07 ──

╭──────────── Summary ────────────╮  ╭────────── Model Usage ──────────╮
│  Hostname       my-macbook      │  │  Model           Output  Cache  │
│  Total Sessions 132             │  │  claude-opus-4-6  780K   519M   │
│  Total Messages 35,330          │  │  claude-sonnet    867K   424M   │
│  Output Tokens  1.6M            │  ╰─────────────────────────────────╯
│  Estimated Cost $1,548         │
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

## Cost Estimation

Calculates estimated API costs using [LiteLLM's pricing DB](https://github.com/BerriAI/litellm) (2,600+ models). Pricing is auto-fetched and cached locally for 24 hours.

- Supports tiered pricing (200K+ token extended context)
- Subagent (haiku) usage included
- Deduplicates streaming message blocks
- Falls back to last cached pricing when offline

```
╭──────────── Cost Breakdown (by month) ────────────╮
│  Month    Model                   Cost             │
│  2026-02  claude-opus-4-6      $236.27             │
│           claude-sonnet-4-5     $50.66             │
│           subtotal             $286.93             │
│                                                    │
│  2026-03  claude-opus-4-6       $30.21             │
│           subtotal              $30.21             │
│                                                    │
│  Total                         $317.14             │
╰────────────────────────────────────────────────────╯
```

## How It Works

Reads local Claude Code data from `~/.claude/`:
- `stats-cache.json` — daily activity, model tokens, hourly counts
- `projects/**/*.jsonl` — session files per project (including subagents)

No API keys required. Local data stays local unless you opt in to sync.

## Roadmap

- [x] Multi-device sync via central server ([#2](https://github.com/hunknownn/claude-multi-usage/issues/2))
- [x] Accurate cost estimation with incremental caching ([#5](https://github.com/hunknownn/claude-multi-usage/issues/5))
- [x] Email-based user isolation for multi-device data ([#8](https://github.com/hunknownn/claude-multi-usage/issues/8))
- [ ] Web dashboard ([#3](https://github.com/hunknownn/claude-multi-usage/issues/3))
- [ ] Homebrew support

## License

MIT
