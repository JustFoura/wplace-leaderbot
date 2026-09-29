# Wplace Leaderbot

A configurable Discord bot that collects Wplace alliance leaderboard data and posts readable reports for selected zones.

The repository contains code, tests, and example configuration only. Runtime state is created locally in `data/` and is ignored by Git.

## Setup

1. Install Python 3.12 and [uv](https://docs.astral.sh/uv/).
2. Run `uv sync`.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Edit `config.json` with your alliance name, zone numbers, country IDs, timezone, and optional known region mappings. The checked-in values are placeholders.
5. Invite the bot with the `bot` and `applications.commands` scopes. It needs permission to view and send messages and embed links in report channels.
6. Run `uv run python bot.py`.

The bot supports multiple Discord servers in one process. Set `DISCORD_GUILD_IDS` to comma or space separated server IDs when developing so commands sync quickly to those servers. Without it, commands are synchronized globally and can take time to appear.

## Commands and permissions

`/leaderboard` is public and shows the latest available report. It supports a configured zone filter and leaderboard mode.

The following server commands require **Manage Server**:

- `/refresh [mode]` collects a fresh snapshot immediately.
- `/settings` and `/schedule-list` show schedules for the current server.
- `/schedule-add` creates a daily report for a channel, local time, timezone, and mode.
- `/schedule-remove` removes a schedule by ID.
- `/schedule-enable` enables or disables a schedule.
- `/schedule-test` previews a configured schedule in its target channel.
- `/channel-set` creates or updates the default daily schedule.
- `/daily-test` previews the default report in the current channel.

Schedules are scoped to their Discord server and can target different channels and timezones. Multiple schedules can share a server or span several servers.

## Command line tools

- `uv run python bot.py resolve` resolves configured zone numbers to Wplace region IDs.
- `uv run python bot.py resolve --deep` performs a wider coordinate sweep when automatic resolution cannot find a zone.
- `uv run python bot.py collect --mode today` collects and prints a snapshot.
- `uv run python bot.py show --mode today` prints the latest stored snapshot.

## Configuration and runtime data

`config.json` controls the tracked alliance, zones, known region IDs, country filters, timezone, report defaults, and snapshot freshness window. Reports use only standard Unicode emoji; no server-specific Discord emoji IDs are required.

The bot creates `data/zone_ids.json`, `data/settings.json`, and `data/snapshots.json` as it runs. Snapshots are retained for up to 90 days and capped at 10,000 entries. Do not commit `data/`, `.env`, Discord tokens, Wplace cookies, or Cloudflare cookies.

## systemd and desktop controls

The files in `deploy/` provide a user systemd unit and optional desktop status controls. They use the example checkout path `%h/wplace-leaderbot`; adjust paths if needed. Install the unit into `~/.config/systemd/user/`, then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now wplace-leaderbot.service
```

## Development

Run the offline checks with:

```sh
uv run python tests/test_offline.py
uv run python -m py_compile bot.py wplace_lb/*.py tests/test_offline.py
```
