# claude-multi-usage (cmu)

CLI dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage tracking across multiple devices.

Parses local `~/.claude` data and displays usage stats in your terminal — sessions, tokens, projects, models, and more. Supports multi-device sync via a central server.

## Install

**Homebrew (macOS)**
```bash
brew tap hunknownn/tap
brew install claude-multi-usage
```

**pipx**
```bash
pipx install claude-multi-usage
```

## Usage

```bash
cmu                    # Full dashboard (last 14 days)
cmu today              # Today's realtime usage
cmu hourly             # Today's hourly breakdown (tokens/msgs per hour)
cmu projects           # Usage by project (sorted by output tokens)
cmu projects -n 5      # Top 5 projects only
cmu models             # Usage by model
cmu cost               # Monthly cost breakdown
cmu dashboard -d 30    # Last 30 days
cmu dashboard --from 2026-03-01 --to 2026-03-07   # Date range
cmu diff               # All devices (per-device view, via sync server)
cmu diff --merged      # All devices (merged into one view)
cmu diff --key my-key  # Filter by specific key
cmu config show        # Show current configuration
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
kubectl create namespace cmu-server
kubectl apply -k https://github.com/hunknownn/claude-multi-usage/deploy/k8s/ -n cmu-server
```

To expose via Ingress (optional):
```bash
curl -O https://raw.githubusercontent.com/hunknownn/claude-multi-usage/main/deploy/k8s/ingress.example.yaml
# Edit the file — replace your-domain.example.com with your domain
kubectl apply -f ingress.example.yaml -n cmu-server
```

**pip**
```bash
pip install claude-multi-usage[server]
cmu server start --host 0.0.0.0 --port 8000
```

### Client Setup

```bash
# Set server URL and key (once per device)
cmu config server https://your-server.com
cmu config key add my-key "personal usage"

# Set device alias (optional, for readable display names)
cmu config alias macbook-air

# Sync usage data
cmu sync

# View all devices' usage
cmu diff               # Per-device view
cmu diff --merged      # Merged into one view
cmu diff --key my-key  # Filter by specific key
```

> Keys are used to group devices. Only devices with the same key can see each other's data. Local commands (`cmu dashboard`, `cmu today`, `cmu cost`, etc.) work without keys.

### Auto Sync

Add to `~/.zshrc` to sync automatically when using Claude:

```bash
cc() {
    cmu sync --quiet &>/dev/null &
    command claude "$@"
    cmu sync --quiet &>/dev/null &
}
```

> The function name `cc` is just an example — you can use any name you prefer (e.g., `cl`, `claude-sync`). If you already have `alias cc="claude"` in your shell config, replace it with the function above and remove the alias line to avoid conflicts.

## Dashboard Preview

### `cmu dashboard` (local)

```
── Claude Usage Dashboard  ──  my-macbook.local  ──  2026-03-07 ──

╭──────────── Summary ────────────╮  ╭──────────── Model Usage ─────────────────────╮
│  Hostname       my-macbook      │  │  Model            Output  Cache Read  Cost   │
│  Total Sessions 132             │  │  claude-opus-4-6   780K     519M    $1,200   │
│  Total Messages 35,330          │  │  claude-sonnet     867K     424M      $348   │
│  First Session  2026-01-15      │  ╰──────────────────────────────────────────────╯
│  Output Tokens  1.6M            │
│  Estimated Cost $1,548          │
╰─────────────────────────────────╯

╭──────────────── Daily Tokens (last 14 days) ─────────────────╮
│  02-23  ████░░░░░░░░░░░░░░░░   60.3K  (1233 msgs, 9 sess)   │
│  02-24  █░░░░░░░░░░░░░░░░░░░   24.8K  (909 msgs, 4 sess)    │
│  03-03  ███████░░░░░░░░░░░░░   96.6K  (633 msgs, 4 sess)    │
╰──────────────────────────────────────────────────────────────╯

╭──────────────── Today Hourly Usage (2026-03-07) ───────────────╮
│    06:00  █████████░░░░░░░░░░░░░   31.5K  (304 msgs, 4 sess)  │
│    07:00  ██████████████████████   79.4K  (707 msgs, 2 sess)  │
│    09:00  ██████████████████████   81.0K  (776 msgs, 7 sess)  │
│  * 13:00  █████████████████████████ 87.2K (676 msgs, 7 sess)  │
│                                                                │
│    Total   398.0K tokens, 3789 messages                        │
╰────────────────────────────────────────────────────────────────╯

╭──────────────── Hourly Sessions (all time) ──────────────────╮
│  00 01 02 .. 09 10 .. 16 17 18 19 20 21 22 23               │
│  ▒▒ ░░ ░░    ░░ ▒▒    ▓▓ ▒▒ ░░ ▒▒ ▓▓ ▓▓ ██ ▓▓               │
╰──────────────────────────────────────────────────────────────╯

╭──────────────── Top Projects ──────────────────────────────────────╮
│  1. apr-backend-assignment   45 sessions   120.5K out  2026-02-10 │
│  2. wemade-assignment        23 sessions    85.2K out  2026-03-04 │
│  3. url-jarvis               17 sessions    42.1K out  2026-03-07 │
╰────────────────────────────────────────────────────────────────────╯
```

### `cmu diff` (multi-device, per-device view)

```
── Claude Diff Dashboard  ──  2 devices  ──  2026-03-07 ──

────────────── donghun (macbook-air.local) ──────────────────

╭──── Summary ────╮  ╭──── Model Usage ────╮
│  Hostname  ...  │  │  Model  Output  .. │
│  Sessions  132  │  │  opus   780K    .. │
╰─────────────────╯  ╰────────────────────╯

╭──── Daily Tokens (last 14 days) ────╮
│  03-03  ████████████████████  96.6K │
╰─────────────────────────────────────╯
╭──── Today Hourly Usage ────────────────────────────╮
│    09:00  ██████████████  81.0K  (776 msgs, 7 sess) │
│  * 13:00  ████████████████ 87.2K (676 msgs, 7 sess) │
│    Total  398.0K tokens, 3789 messages               │
╰──────────────────────────────────────────────────────╯
╭──── Top Projects ───╮
│  ...                 │
╰──────────────────────╯

──────────────────── office-desktop.local ───────────────────

╭──── Summary ────╮  ╭──── Model Usage ────╮
│  ...            │  │  ...                │
╰─────────────────╯  ╰────────────────────╯
╭──── Daily Tokens ───╮
╭──── Today Hourly Usage ───╮
╭──── Top Projects ───╮
```

> Devices with an alias show as `alias (hostname)`. Devices without an alias show hostname only. Today's hourly usage is shown per device when available via `cmu sync`.

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
- [x] Key-based device grouping with alias support ([#8](https://github.com/hunknownn/claude-multi-usage/issues/8), [#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `cmu diff` — multi-device per-device/merged view ([#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `cmu hourly` — today's per-hour usage breakdown with sync support
- [x] Homebrew support
- [ ] Web dashboard ([#3](https://github.com/hunknownn/claude-multi-usage/issues/3))

## License

MIT
