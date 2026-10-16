# sandburn (sburn)

CLI dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage tracking across multiple devices.

Parses local `~/.claude` data and displays usage stats in your terminal — sessions, tokens, projects, models, and more. Supports multi-device sync via a central server.

## Install

This fork is installed from the repository with [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`); no system Python, pip or venv package is needed, uv downloads a Python if there is none. The upstream package on PyPI and Homebrew (`pipx install claude-multi-usage`, `brew install hunknownn/tap/claude-multi-usage`) is upstream's release line and does not contain this fork's fixes (token deduplication, repository-name projects, the server's size limit).

```bash
uv tool install "git+https://github.com/mattculler/sandburn.git"
uv tool upgrade sandburn          # later, to pick up new commits
```

From a checkout, `uv tool install -e .` installs it editable: `git pull` is then enough to update, and `sburn remote install` sends the checkout itself to other machines.

Development happens on Forgejo; GitHub (`mattculler/sandburn`) is a mirror of it, and the address the install commands use.

### Upgrading from claude-multi-usage

This tool used to be `claude-multi-usage`, with the command `cmu`. Installing sandburn replaces it:

- **Workstation:** `uv tool install "git+https://github.com/mattculler/sandburn.git"`, then `uv tool uninstall claude-multi-usage`. The first `sburn` run moves `~/.claude-multi-usage` (settings, session index, autosync state) to `~/.sandburn`, and `sburn autosync install` replaces the old `cmu-autosync` timer or `com.claude-multi-usage.autosync` agent, which would otherwise keep calling the removed command.
- **Other machines:** `sburn remote install HOST` does all of the above on the remote.
- **Server VM:** `git pull` and `sudo deploy/systemd/install.sh`. It retires `cmu-server.service`, removes `/opt/cmu` and moves the database from `/var/lib/cmu` to `/var/lib/sandburn`. Settings in `/etc/default/cmu-server` are not carried over: the variables are now `SANDBURN_HOST`, `SANDBURN_PORT`, `SANDBURN_DB_PATH` and `SANDBURN_MAX_BODY_BYTES`, in `/etc/default/sandburn-server`. The `cmu` user and the empty `/var/lib/cmu` are left for you to delete.
- **Docker and Kubernetes:** the image still keeps its data in `/data`, but the example names changed. Stop and remove the old `cmu-server` container and keep mounting your existing volume (`-v cmu-data:/data`), or copy it to `sandburn-data`. On Kubernetes the claim is now `sandburn-server-data`: keep `claimName: cmu-server-data` in `deployment.yaml` or move the data over. `CMU_*` environment variables became `SANDBURN_*`.

## Usage

```bash
sburn                    # Full dashboard (last 14 days)
sburn today              # Today's realtime usage
sburn hourly             # Today's hourly breakdown (tokens/msgs per hour)
sburn projects           # Usage by project (sorted by output tokens)
sburn projects -n 5      # Top 5 projects only
sburn models             # Usage by model
sburn cost               # Monthly cost breakdown
sburn cost --rebuild     # Re-read the session files on disk (after a time zone change)
sburn tree               # Grass/tree ASCII art visualization (daily/weekly/monthly/yearly)
sburn dashboard -d 30    # Last 30 days
sburn dashboard --from 2026-03-01 --to 2026-03-07   # Date range
sburn diff               # All devices (per-device view, via sync server)
sburn diff --merged      # All devices (merged into one view)
sburn diff --key my-key  # Filter by specific key
sburn config show        # Show current configuration
sburn sync               # Push local usage to the sync server once
sburn autosync install   # Sync automatically whenever local usage changes
sburn remote install HOST                # Install sburn + autosync on another machine over ssh
sburn import-claude-export export.zip    # Import a claude.ai data export (web/desktop/mobile chats)
```

`sandburn` also works as a command alias.

## Multi-Device Sync

Collect usage data from multiple machines into a central server.

> **Warning: the server has no authentication.** Anyone who can reach its port can read every device's data, overwrite any device's data, or add themselves to any key group. Run it only on a private network (LAN, VPN, Tailscale) and never expose it to the Internet. Keys group devices for display; they are labels, not credentials.

### What gets synced

`sburn sync` sends: hostname, alias, your keys, the sync time, cumulative session and message totals and the first-session date, per-day message/session/tool-call counts, per-day and cumulative token counts by model, an hourly session histogram, today's per-hour tokens/messages/sessions, and a project list. Projects are identified by repository name (the last component of the working directory Claude Code ran in; a Claude Code worktree counts as its repository), with session counts, token totals and first/last-seen dates. It never sends prompts, responses, file contents, full paths, or API keys.

For very old session logs that recorded no working directory, the name is guessed from Claude Code's directory name instead and may be truncated (for example `my-repo` becomes `repo`). Servers that received syncs from clients older than this fork keep the full-path names those clients sent until the server is restarted or the device syncs again; both drop them.

### Server Setup

**Run directly on a VM (systemd)**

On a Linux host with systemd and `curl`, from a checkout of this repository:

```bash
git clone https://github.com/mattculler/sandburn.git
cd sandburn
sudo deploy/systemd/install.sh
```

The script installs uv into `/usr/local/bin` if the host has none, creates a `sandburn` system user, builds `/opt/sandburn/venv` from `uv.lock` on the host's Python 3.13 (or one uv downloads into `/opt/sandburn/python`; `sudo PYTHON=3.12 deploy/systemd/install.sh` picks another, since sudo resets the environment the variable goes after `sudo`), keeps the database in `/var/lib/sandburn/server.db`, and enables `sandburn-server.service` on port 8000, then waits for `/api/health` to answer. Settings (bind address, port, database path, request size limit) live in `/etc/default/sandburn-server`; restart the service after editing. A different database directory must exist and be writable by the `sandburn` user. To upgrade, `git pull` and re-run the script. To remove it, `sudo deploy/systemd/install.sh --uninstall`.

```bash
systemctl status sandburn-server
journalctl -u sandburn-server -f
curl http://localhost:8000/api/health
```

**Other ways to run it**

<details>
<summary>Docker</summary>

Build the image from this checkout; the published `ghcr.io/hunknownn/claude-multi-usage` image is upstream's build and lacks this fork's changes.

```bash
docker build -t sandburn-server .
docker run -d --name sandburn-server -p 127.0.0.1:8000:8000 -v sandburn-data:/data sandburn-server
```

Replace `127.0.0.1` with the LAN address clients should use. Do not publish the port on a public interface.
</details>

<details>
<summary>Kubernetes</summary>

`deploy/k8s/` has a Kustomize base (Deployment, ClusterIP Service, 1 GiB PVC). Its `kustomization.yaml` pins upstream's `ghcr.io/hunknownn/claude-multi-usage` image; to run this fork's code, build and push your own image and point the `images:` entry at it, then:

```bash
kubectl create namespace sandburn-server
kubectl apply -k deploy/k8s/ -n sandburn-server
```

The Service is `ClusterIP`, reachable inside the cluster only. `deploy/k8s/ingress.example.yaml` shows how to expose it through an Ingress; only do that behind an authenticating ingress or on a private network.
</details>

<details>
<summary>uv, in the foreground</summary>

From a checkout of this repository:

```bash
uv run --extra server sburn server start --db-path ./server.db
uv run --extra server sburn server start --host 0.0.0.0 --port 8000 --db-path ./server.db
```

Options: `--host`, `--port`, `--db-path PATH` (default `/data/server.db`, the Docker image's volume), `--max-body-bytes N` (reject sync payloads larger than N bytes; default 2 MB). The same settings can be given as `SANDBURN_DB_PATH` and `SANDBURN_MAX_BODY_BYTES` environment variables.
</details>

### Client Setup

```bash
# Set server URL and key (once per device)
sburn config server https://your-server.com
sburn config key add my-key "personal usage"

# Set device alias (optional, for readable display names)
sburn config alias macbook-air

# Sync usage data
sburn sync

# View all devices' usage
sburn diff               # Per-device view
sburn diff --merged      # Merged into one view
sburn diff --key my-key  # Filter by specific key
```

> Keys group devices for the `sburn diff` view. They are not access control: the server returns every device's data to anyone who asks (see the warning above). Local commands (`sburn dashboard`, `sburn today`, `sburn cost`, etc.) work without keys or a server.

### Auto Sync

Install a per-user scheduled job that checks every few minutes whether the local usage files changed and uploads only then:

```bash
sburn autosync install            # every 15 minutes
sburn autosync install --every 5
sburn autosync status
sburn autosync uninstall
```

On Linux this is a systemd user timer (`~/.config/systemd/user/sandburn-autosync.timer`); it runs while you are logged in, or always with `sburn autosync install --linger` (`loginctl enable-linger`). On macOS it is a launchd agent in `~/Library/LaunchAgents`, logging to `~/.sandburn/autosync.log`. The job runs `sburn autosync run`, which compares a fingerprint of `~/.claude` and the sync settings with the one from the last successful sync; an idle machine never contacts the server. `sburn sync --if-changed` does the same check for a shell wrapper such as:

```bash
cc() {
    sburn sync --if-changed --quiet &>/dev/null &
    command claude "$@"
    sburn sync --if-changed --quiet &>/dev/null &
}
```

### Other machines

From a machine that is already configured, install sburn, the server settings and the autosync job on another one over ssh:

```bash
sburn remote install alice@laptop
sburn remote install laptop --alias laptop --every 5
sburn remote install laptop --from git+https://github.com/mattculler/sandburn.git
sburn remote uninstall laptop
```

The remote gets uv (installed into `~/.local/bin` by the official installer if the host has none; shell profiles are left alone, so that directory must be on the PATH there), sburn as a uv tool with a `~/.local/bin/sburn` command, this machine's server URL and keys (override with `--server` and `--key`), and `sburn autosync install` (add `--linger` for a Linux remote that should sync while nobody is logged in). When run from a source checkout, the checkout's git-tracked files are sent over and kept in `~/.local/share/sandburn/src`; otherwise the repository URL is installed, which needs `git` and GitHub access on the remote. The remote needs `bash`, `curl` or `wget`, and access to PyPI for the dependencies; uv downloads a Python if the host has none that is 3.9 or newer (pick one with `--python 3.12`). Re-running the command upgrades an existing install; one from before the rename (`claude-multi-usage`, `cmu`) is removed, including its autosync job. On a Mac that nobody is logged in to, the launchd agent is installed but only starts at the next login. Make sure the server URL is one the remote can reach (not `localhost`).

### claude.ai chats (web, desktop, Android, iOS)

Chats in the Claude apps are not on any machine you control, and Anthropic offers no usage API for subscribers. The compliant source is the account data export: on claude.ai (web or desktop app; not available in the mobile apps) go to Settings > Privacy > Export data, download the ZIP from the emailed link (valid 24 hours), then:

```bash
sburn import-claude-export ~/Downloads/data-2026-10-06.zip
sburn import-claude-export ~/Downloads/data-2026-10-06.zip --dry-run   # inspect without sending
```

The export is account-wide, so it covers conversations from every client including the phone apps. Each account is imported as its own device, `claude.ai-<id>` (alias `claude.ai`, change it with `--alias`), visible in `sburn diff`. Message and conversation counts and dates are exact; the export has no token counts or model names, so tokens are estimated from text length (about 4 characters per token, so non-English text is underestimated) under the model name `claude.ai (estimated)`, and the merged view marks totals that include them. An export is a complete snapshot of the account, so each import replaces the previous one on the server (older servers without snapshot support merge day by day instead): the most recent import wins. Days are bucketed in the local time zone of the machine that runs the import. The export cannot be automated or scheduled: request it by hand at whatever cadence you like.

## Dashboard Preview

### `sburn dashboard` (local)

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

### `sburn diff` (multi-device, per-device view)

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

> Devices with an alias show as `alias (hostname)`. Devices without an alias show hostname only. Today's hourly usage is shown per device when available via `sburn sync`.

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

Session files are read into an incremental index at `~/.sandburn/index.db`. A file is read again only when it changes, and only the newly appended bytes when it grew in place, so commands stay fast as history accumulates. Transcripts that Claude Code deletes after its retention period stay in the index as history, so `sburn cost` keeps showing past months. The index stores token counts, not prices. Local dates are fixed when a file is indexed, so after changing the system time zone run `sburn cost --rebuild` (it re-reads the files still on disk; already-deleted transcripts keep their old dates). If the index cannot be written (read-only home directory) the session files are read directly for that run.

Upgrading from a version that kept `~/.sandburn/cost-cache.json`: that file is no longer read and can be deleted. Days whose transcripts Claude Code had already deleted before the first run of this version are not carried over.

No API keys required. Local data stays local unless you opt in to sync; the only other network access is a fetch of model prices from LiteLLM's GitHub repository, cached for 24 hours.

## Roadmap

- [x] Multi-device sync via central server ([#2](https://github.com/hunknownn/claude-multi-usage/issues/2))
- [x] Accurate cost estimation with incremental caching ([#5](https://github.com/hunknownn/claude-multi-usage/issues/5))
- [x] Key-based device grouping with alias support ([#8](https://github.com/hunknownn/claude-multi-usage/issues/8), [#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `sburn diff` — multi-device per-device/merged view ([#11](https://github.com/hunknownn/claude-multi-usage/issues/11))
- [x] `sburn hourly` — today's per-hour usage breakdown with sync support
- [x] `sburn tree` — grass/tree ASCII art visualization ([#17](https://github.com/hunknownn/claude-multi-usage/issues/17))
- [x] Homebrew support
- [ ] Web dashboard ([#3](https://github.com/hunknownn/claude-multi-usage/issues/3))

## Development

```bash
uv sync --extra server    # .venv with the package (editable), the server and the dev tools
uv run ruff check         # lint (rules in pyproject.toml)
uv run ruff format        # format
uv run pytest
```

CI (`.github/workflows/ci.yml`, run by GitHub Actions on the mirror) has a lint job (`ruff check` and `ruff format --check`) and a test job (`pytest` on 3.9, 3.11 and 3.13), both installed from the lockfile with `--locked`, which fails when `uv.lock` is stale: run `uv lock` after changing dependencies and commit `uv.lock`.

Tests run against a synthetic `~/.claude` tree in a temp directory and never touch the network.

## License

MIT
