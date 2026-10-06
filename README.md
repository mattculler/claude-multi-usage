# claude-multi-usage (cmu)

CLI dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage tracking across multiple devices.

Parses local `~/.claude` data and displays usage stats in your terminal — sessions, tokens, projects, models, and more. Supports multi-device sync via a central server.

## Install

This fork is installed from the repository. The upstream package on PyPI and Homebrew (`pipx install claude-multi-usage`, `brew install hunknownn/tap/claude-multi-usage`) is upstream's release line and does not contain this fork's fixes (token deduplication, repository-name projects, the server's size limit).

**pipx**
```bash
pipx install "git+https://github.com/mattculler/claude-multi-usage.git"
```

**pip, from a checkout**
```bash
git clone https://github.com/mattculler/claude-multi-usage.git
cd claude-multi-usage
pip install .            # or: pip install ".[server]" to include the sync server
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
cmu cost --rebuild     # Re-read all session files (after a time zone change)
cmu tree               # Grass/tree ASCII art visualization (daily/weekly/monthly/yearly)
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

> **Warning: the server has no authentication.** Anyone who can reach its port can read every device's data, overwrite any device's data, or add themselves to any key group. Run it only on a private network (LAN, VPN, Tailscale) and never expose it to the Internet. Keys group devices for display; they are labels, not credentials.

### What gets synced

`cmu sync` sends: hostname, alias, your keys, the sync time, cumulative session and message totals and the first-session date, per-day message/session/tool-call counts, per-day and cumulative token counts by model, an hourly session histogram, today's per-hour tokens/messages/sessions, and a project list. Projects are identified by repository name (the last component of the working directory Claude Code ran in; a Claude Code worktree counts as its repository), with session counts, token totals and first/last-seen dates. It never sends prompts, responses, file contents, full paths, or API keys.

For very old session logs that recorded no working directory, the name is guessed from Claude Code's directory name instead and may be truncated (for example `my-repo` becomes `repo`). Servers that received syncs from clients older than this fork keep the full-path names those clients sent until the server is restarted or the device syncs again; both drop them.

### Server Setup

**Run directly on a VM (systemd)**

On a Linux host with systemd and Python 3.9+ (on Debian/Ubuntu also `apt install python3-venv`), from a checkout of this repository:

```bash
git clone https://github.com/mattculler/claude-multi-usage.git
cd claude-multi-usage
sudo deploy/systemd/install.sh
```

The script creates a `cmu` system user, installs the package into `/opt/cmu/venv`, keeps the database in `/var/lib/cmu/server.db`, and enables `cmu-server.service` on port 8000, then waits for `/api/health` to answer. Settings (bind address, port, database path, request size limit) live in `/etc/default/cmu-server`; restart the service after editing. A different database directory must exist and be writable by the `cmu` user. To upgrade, `git pull` and re-run the script. To remove it, `sudo deploy/systemd/install.sh --uninstall`.

```bash
systemctl status cmu-server
journalctl -u cmu-server -f
curl http://localhost:8000/api/health
```

**Other ways to run it**

<details>
<summary>Docker</summary>

Build the image from this checkout; the published `ghcr.io/hunknownn/claude-multi-usage` image is upstream's build and lacks this fork's changes.

```bash
docker build -t cmu-server .
docker run -d --name cmu-server -p 127.0.0.1:8000:8000 -v cmu-data:/data cmu-server
```

Replace `127.0.0.1` with the LAN address clients should use. Do not publish the port on a public interface.
</details>

<details>
<summary>Kubernetes</summary>

`deploy/k8s/` has a Kustomize base (Deployment, ClusterIP Service, 1 GiB PVC). Its `kustomization.yaml` pins upstream's `ghcr.io/hunknownn/claude-multi-usage` image; to run this fork's code, build and push your own image and point the `images:` entry at it, then:

```bash
kubectl create namespace cmu-server
kubectl apply -k deploy/k8s/ -n cmu-server
```

The Service is `ClusterIP`, reachable inside the cluster only. `deploy/k8s/ingress.example.yaml` shows how to expose it through an Ingress; only do that behind an authenticating ingress or on a private network.
</details>

<details>
<summary>pip, in the foreground</summary>

From a checkout of this repository:

```bash
pip install ".[server]"
cmu server start --host 0.0.0.0 --port 8000 --db-path ./server.db
```

Options: `--host`, `--port`, `--db-path PATH` (default `/data/server.db`), `--max-body-bytes N` (reject sync payloads larger than N bytes; default 2 MB). The same settings can be given as `CMU_DB_PATH` and `CMU_MAX_BODY_BYTES` environment variables.
</details>

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

> Keys group devices for the `cmu diff` view. They are not access control: the server returns every device's data to anyone who asks (see the warning above). Local commands (`cmu dashboard`, `cmu today`, `cmu cost`, etc.) work without keys or a server.

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

- Prices are applied when displaying, so a pricing update applies to all history immediately
- Supports tiered pricing (200K+ token extended context)
- Subagent usage included
- Deduplicates streaming message blocks and resumed-session copies
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

Session files are read into an incremental index at `~/.claude-multi-usage/index.db`. A file is read again only when it changes, and only the newly appended bytes when it grows, so commands stay fast as history accumulates; files Claude Code deletes drop out of the index. The index stores token counts, not prices. Local dates are fixed when a file is indexed, so after changing the system time zone run `cmu cost --rebuild`.

No API keys required. Local data stays local unless you opt in to sync; the only other network access is a fetch of model prices from LiteLLM's GitHub repository, cached for 24 hours.

## Roadmap

- [x] Multi-device sync via central server ([#2](https://github.com/hunknownn/claude-multi-usage/issues/2))
- [x] Accurate cost estimation with incremental caching ([#5](https://github.com/hunknownn/claude-multi-usage/issues/5))
- [x] Key-based device grouping with alias support ([#8](https://github.com/hunknownn/claude-multi-usage/issues/8), [#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `cmu diff` — multi-device per-device/merged view ([#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `cmu hourly` — today's per-hour usage breakdown with sync support
- [x] `cmu tree` — grass/tree ASCII art visualization ([#17](https://github.com/hunknownn/claude-multi-usage/issues/17))
- [x] Homebrew support
- [ ] Web dashboard ([#3](https://github.com/hunknownn/claude-multi-usage/issues/3))

## Development

```bash
pip install -e ".[server,dev]"
ruff check --select F claude_multi_usage tests
pytest
```

Tests run against a synthetic `~/.claude` tree in a temp directory and never touch the network.

## License

MIT
