from __future__ import annotations

import asyncio
import logging
import time as time_mod
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .api import MODES, WplaceClient, WplaceError, jwt_expiry
from .collector import collect_and_store, snapshot_age_seconds
from .config import Config, Secrets
from .render import build_embed, build_zone_embed
from .storage import SettingsStore, SnapshotStore, ZoneStore
from .zones import AllianceIdentity, ZoneResolver, alliance_identity

log = logging.getLogger('wplace.bot')
REFRESH_COOLDOWN_SECONDS = 60


def parse_hhmm(value: str) -> tuple[int, int]:
    hour_s, separator, minute_s = value.strip().partition(':')
    if not separator:
        raise ValueError(f'{value!r} is not HH:MM')
    try:
        hour, minute = (int(hour_s), int(minute_s))
    except ValueError as exc:
        raise ValueError(f'{value!r} is not HH:MM') from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f'{value!r}: hour must be 0-23 and minute 0-59')
    return (hour, minute)


def next_run_at(hour: int, minute: int, tz: ZoneInfo) -> datetime:
    now = datetime.now(tz)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _schedule_next_run(schedule: dict, now_utc: datetime | None=None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo(schedule['timezone']))
    hour, minute = parse_hhmm(schedule['time'])
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _schedule_text(schedule: dict) -> str:
    status = 'enabled' if schedule['enabled'] else 'disabled'
    return f"`{schedule['id']}` **{schedule['name']}** · {schedule['time']} {schedule['timezone']} · {schedule['mode']} · <#{schedule['channel_id']}> · {status}"


class WplaceLeaderboardBot:

    def __init__(self, config: Config, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets
        self.zones = ZoneStore(config.data_dir / 'zone_ids.json')
        self.snapshots = SnapshotStore(config.data_dir / 'snapshots.json')
        self.settings = SettingsStore(config.data_dir / 'settings.json')
        for guild_id in secrets.discord_guild_ids:
            self.settings.migrate_legacy(
                guild_id=guild_id,
                fallback_channel_id=secrets.discord_channel_id,
                default_time=config.daily_report_time,
                default_timezone=config.timezone,
                default_mode=config.report_mode,
            )
        self.alliance = AllianceIdentity(None, config.alliance_name, None)
        self.api: WplaceClient | None = None
        self._session: Any = None
        self._resolve_lock = asyncio.Lock()
        self._collect_lock = asyncio.Lock()
        self._schedule_wakeup = asyncio.Event()
        self._scheduler_task: asyncio.Task | None = None
        self._startup: asyncio.Task | None = None
        self._last_refresh = 0.0
        self.client = discord.Client(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self.client)
        self._ready_done = False
        self._register_commands()
        self._register_events()

    def eff_report_time(self) -> tuple[int, int]:
        return parse_hhmm(self.config.daily_report_time)

    def eff_timezone(self) -> ZoneInfo:
        return ZoneInfo(self.config.timezone)

    def eff_report_mode(self) -> str:
        return self.config.report_mode

    def eff_channel_id(self) -> int | None:
        return self.secrets.discord_channel_id

    def eff_schedule(self):
        hour, minute = self.eff_report_time()
        return dt_time(hour, minute, tzinfo=self.eff_timezone())

    def schedules_for_guild(self, guild_id: int) -> list[dict]:
        return self.settings.list_schedules(guild_id)

    def schedule_list_text(self, guild_id: int) -> str:
        schedules = self.schedules_for_guild(guild_id)
        if not schedules:
            return 'No report schedules are configured for this server. Use `/schedule-add` to create one.'
        lines = [f'Report schedules for server `{guild_id}`:']
        lines.extend(f'{index}. {_schedule_text(schedule)}' for index, schedule in enumerate(schedules, 1))
        text = '\n'.join(lines)
        return text[:1950] + ('\n…list truncated.' if len(text) > 1950 else '')

    async def start(self) -> None:
        import aiohttp

        if self.secrets.wplace_j:
            expiry = jwt_expiry(self.secrets.wplace_j)
            if expiry is not None:
                log.info('WPLACE_J cookie expires: %s', expiry.isoformat(timespec='seconds'))
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.api = WplaceClient(self._session, j_cookie=self.secrets.wplace_j, cf_clearance=self.secrets.wplace_cf_clearance, user_agent=self.secrets.wplace_user_agent)
        try:
            self.alliance = await alliance_identity(self.api, self.config, self.secrets.wplace_j)
        except WplaceError as exc:
            log.warning('could not identify the alliance at startup: %s; continuing anonymously', exc)
            self.alliance = AllianceIdentity(None, self.config.alliance_name, None)
        if self.alliance.cookie_valid is False:
            log.warning('WPLACE_J was rejected by /me (expired or invalid). Running anonymously; paste a fresh cookie into .env to re-enable alliance-aware resolution.')
        else:
            log.info('tracking alliance %s (id=%s)', self.alliance.name, self.alliance.alliance_id or 'unknown — matching by name')
        try:
            await self.client.start(self.secrets.discord_token)
        finally:
            await self._stop_background_work()
            if self._session is not None:
                await self._session.close()

    async def close(self) -> None:
        await self._stop_background_work()
        if self._session is not None:
            await self._session.close()

    def _register_events(self) -> None:
        bot = self

        @self.client.event
        async def on_ready() -> None:
            log.info('logged in as %s', bot.client.user)
            if bot._ready_done:
                return
            guild_ids = bot.secrets.discord_guild_ids
            if not guild_ids and len(bot.client.guilds) == 1:
                bot.settings.migrate_legacy(
                    guild_id=bot.client.guilds[0].id,
                    fallback_channel_id=bot.secrets.discord_channel_id,
                    default_time=bot.config.daily_report_time,
                    default_timezone=bot.config.timezone,
                    default_mode=bot.config.report_mode,
                )
            sync_ok = True
            try:
                if guild_ids:
                    for guild_id in guild_ids:
                        try:
                            guild = discord.Object(id=guild_id)
                            bot.tree.copy_global_to(guild=guild)
                            await bot.tree.sync(guild=guild)
                            log.info('slash commands synced to guild %s', guild_id)
                        except Exception:
                            sync_ok = False
                            log.exception('slash command sync failed for guild %s', guild_id)
                else:
                    await bot.tree.sync()
                    log.info('slash commands synced globally')
            except Exception:
                sync_ok = False
                log.exception('slash command sync failed')
            bot._ready_done = sync_ok
            bot.start_background_work()

    def _register_commands(self) -> None:
        bot = self
        zone_choices = [app_commands.Choice(name=f'#{z}', value=z) for z in self.config.zones]
        mode_choices = [app_commands.Choice(name=m, value=m) for m in MODES]

        async def schedule_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            if interaction.guild_id is None:
                return []
            query = current.casefold()
            choices = []
            for schedule in bot.schedules_for_guild(interaction.guild_id):
                haystack = f"{schedule['id']} {schedule['name']} {schedule['time']}".casefold()
                if query and query not in haystack:
                    continue
                label = f"{schedule['id']} · {schedule['name']} · {schedule['time']} {schedule['timezone']}"[:100]
                choices.append(app_commands.Choice(name=label, value=schedule['id']))
            return choices[:25]

        @self.tree.command(name='leaderboard', description='View the latest Wplace pixel counts for our alliance')
        @app_commands.choices(mode=mode_choices)
        @app_commands.choices(zone=zone_choices)
        async def leaderboard(interaction: discord.Interaction, zone: app_commands.Choice[int] | None=None, mode: app_commands.Choice[str] | None=None) -> None:
            await interaction.response.defer(thinking=True)
            chosen_mode = mode.value if mode else bot.config.default_mode
            snapshot = bot.snapshots.latest(chosen_mode)
            if snapshot is None or snapshot_age_seconds(snapshot) > bot.config.stale_snapshot_hours * 3600:
                try:
                    snapshot = await bot.collect_if_stale(chosen_mode)
                except Exception:
                    log.exception('on-demand collection failed')
            if snapshot is None:
                await interaction.followup.send('No snapshot is available yet and live collection failed. Try again shortly.')
                return
            zone_filter = zone.value if zone is not None else None
            previous = bot.snapshots.previous(chosen_mode)
            if zone_filter is not None:
                await interaction.followup.send(embed=build_zone_embed(snapshot, bot.config, zone_filter))
            else:
                await interaction.followup.send(embed=build_embed(snapshot, bot.config, None, previous))

        @self.tree.command(name='refresh', description='Force a fresh Wplace collection now (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.choices(mode=mode_choices)
        async def refresh(interaction: discord.Interaction, mode: app_commands.Choice[str] | None=None) -> None:
            elapsed = time_mod.monotonic() - bot._last_refresh
            if elapsed < REFRESH_COOLDOWN_SECONDS:
                remaining = int(REFRESH_COOLDOWN_SECONDS - elapsed) + 1
                await interaction.response.send_message(f'Try again in {remaining}s.', ephemeral=True)
                return
            bot._last_refresh = time_mod.monotonic()
            await interaction.response.defer(thinking=True)
            chosen_mode = mode.value if mode else bot.config.report_mode
            snapshot = await bot.collect(chosen_mode)
            previous = bot.snapshots.previous(snapshot['mode'])
            await interaction.followup.send(embed=build_embed(snapshot, bot.config, previous=previous))

        @self.tree.command(name='settings', description='View report schedules for this server (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def settings(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(bot.schedule_list_text(interaction.guild_id), ephemeral=True)

        @self.tree.command(name='schedule-add', description='Add a daily report schedule (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.choices(mode=mode_choices)
        async def schedule_add(interaction: discord.Interaction, report_time: str, channel: discord.TextChannel, timezone: str | None=None, mode: app_commands.Choice[str] | None=None, name: str | None=None) -> None:
            try:
                parse_hhmm(report_time)
            except ValueError as exc:
                await interaction.response.send_message(f'Bad time: {exc}', ephemeral=True)
                return
            timezone_value = timezone or bot.config.timezone
            try:
                ZoneInfo(timezone_value)
            except Exception:
                await interaction.response.send_message(f'Unknown timezone {timezone_value!r}. Use an IANA timezone such as `America/New_York` or `UTC`.', ephemeral=True)
                return
            schedule = bot.settings.add_schedule(
                guild_id=interaction.guild_id,
                channel_id=channel.id,
                name=(name or f'Daily {report_time}'),
                report_time=report_time,
                timezone=timezone_value,
                mode=mode.value if mode else bot.config.report_mode,
            )
            bot.reschedule_daily()
            await interaction.response.send_message(f'Added {_schedule_text(schedule)}.', ephemeral=True)

        @self.tree.command(name='schedule-list', description='List daily report schedules (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def schedule_list(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(bot.schedule_list_text(interaction.guild_id), ephemeral=True)

        @self.tree.command(name='schedule-remove', description='Remove a daily report schedule (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.autocomplete(schedule_id=schedule_autocomplete)
        async def schedule_remove(interaction: discord.Interaction, schedule_id: str) -> None:
            if bot.settings.get_schedule(schedule_id, interaction.guild_id) is None:
                await interaction.response.send_message('That schedule was not found in this server. Use `/schedule-list` to see valid IDs.', ephemeral=True)
                return
            bot.settings.remove_schedule(schedule_id, interaction.guild_id)
            bot.reschedule_daily()
            await interaction.response.send_message(f'Removed schedule `{schedule_id}`.', ephemeral=True)

        @self.tree.command(name='schedule-enable', description='Enable or disable a daily report schedule (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.autocomplete(schedule_id=schedule_autocomplete)
        async def schedule_enable(interaction: discord.Interaction, schedule_id: str, enabled: bool) -> None:
            schedule = bot.settings.update_schedule(schedule_id, interaction.guild_id, {'enabled': enabled})
            if schedule is None:
                await interaction.response.send_message('That schedule was not found in this server. Use `/schedule-list` to see valid IDs.', ephemeral=True)
                return
            bot.reschedule_daily()
            state = 'enabled' if enabled else 'disabled'
            await interaction.response.send_message(f'Schedule `{schedule_id}` is now **{state}**.', ephemeral=True)

        @self.tree.command(name='schedule-test', description='Preview a configured daily report schedule (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.autocomplete(schedule_id=schedule_autocomplete)
        async def schedule_test(interaction: discord.Interaction, schedule_id: str | None=None) -> None:
            schedule = bot.settings.get_schedule(schedule_id, interaction.guild_id) if schedule_id else None
            if schedule_id and schedule is None:
                await interaction.response.send_message('That schedule was not found in this server. Use `/schedule-list` to see valid IDs.', ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            mode = schedule['mode'] if schedule else bot.config.report_mode
            try:
                snapshot = await bot.collect(mode)
                channel = await bot.resolve_report_channel(schedule['channel_id']) if schedule else interaction.channel
                if channel is None or not hasattr(channel, 'send'):
                    await interaction.followup.send('The target channel could not be accessed.', ephemeral=True)
                    return
                await interaction.followup.send(f'Posting the {mode} preview to {channel.mention if hasattr(channel, "mention") else "the current channel"}.', ephemeral=True)
                sent = await bot.post_daily_sequence(channel, snapshot)
                await interaction.followup.send(f'Preview complete: {sent}/{len(bot.config.zones)} zone messages sent.', ephemeral=True)
            except Exception:
                log.exception('schedule test failed')
                await interaction.followup.send('The schedule preview failed. Check the bot log for details.', ephemeral=True)

        @self.tree.command(name='channel-set', description='Set or replace this server’s default daily report (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def channel_set(interaction: discord.Interaction, channel: discord.TextChannel | None=None) -> None:
            target = channel or interaction.channel
            if not isinstance(target, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message('Select a text channel.', ephemeral=True)
                return
            schedules = bot.schedules_for_guild(interaction.guild_id)
            existing = next((item for item in schedules if item['name'].casefold() == 'daily'), None)
            if existing:
                schedule = bot.settings.update_schedule(existing['id'], interaction.guild_id, {'channel_id': target.id})
            else:
                schedule = bot.settings.add_schedule(guild_id=interaction.guild_id, channel_id=target.id, name='Daily', report_time=bot.config.daily_report_time, timezone=bot.config.timezone, mode=bot.config.report_mode)
            bot.reschedule_daily()
            await interaction.response.send_message(f'Default report set to {_schedule_text(schedule)}.', ephemeral=True)

        @self.tree.command(name='daily-test', description='Preview the default report in this channel (Manage Server)')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def daily_test(interaction: discord.Interaction) -> None:
            await interaction.response.defer(thinking=True)
            try:
                snapshot = await bot.collect(bot.config.report_mode)
                await interaction.followup.send(f'Rehearsing the daily sequence ({len(bot.config.zones)} zones, ~5s apart)…')
                sent = await bot.post_daily_sequence(interaction.channel, snapshot)
                await interaction.followup.send(f'Preview complete: {sent}/{len(bot.config.zones)} zone messages sent.', ephemeral=True)
            except Exception:
                log.exception('daily-test collection failed')
                await interaction.followup.send('The preview failed. Check the bot log for details.', ephemeral=True)

        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            if isinstance(error, app_commands.errors.MissingPermissions):
                message = 'You need the **Manage Server** permission to use this command.'
            elif isinstance(error, app_commands.errors.NoPrivateMessage):
                message = 'This command can only be used inside a server.'
            elif isinstance(error, app_commands.errors.CommandOnCooldown):
                message = f'Try again in {error.retry_after:.0f}s.'
            else:
                log.error('command error: %s', error, exc_info=error)
                message = 'Command failed. Check the bot log for details.'
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def collect(self, mode: str) -> dict:
        if self.api is None:
            raise RuntimeError('bot not started')
        async with self._collect_lock:
            await self.ensure_zones()
            return await collect_and_store(self.api, self.config, self.zones, self.snapshots, mode, self.alliance.alliance_id, self.alliance.name)

    async def collect_if_stale(self, mode: str, force: bool=False) -> dict:
        if not force:
            snapshot = self.snapshots.latest(mode)
            if snapshot is not None and snapshot_age_seconds(snapshot) <= self.config.stale_snapshot_hours * 3600:
                return snapshot
        async with self._collect_lock:
            snapshot = self.snapshots.latest(mode)
            if not force and snapshot is not None and snapshot_age_seconds(snapshot) <= self.config.stale_snapshot_hours * 3600:
                return snapshot
            await self.ensure_zones()
            return await collect_and_store(self.api, self.config, self.zones, self.snapshots, mode, self.alliance.alliance_id, self.alliance.name)

    async def ensure_zones(self) -> None:
        if self.api is None:
            raise RuntimeError('bot not started')
        unresolved = [z for z in self.config.zones if self.zones.get(z) is None]
        if not unresolved:
            return
        async with self._resolve_lock:
            unresolved = [z for z in self.config.zones if self.zones.get(z) is None]
            if not unresolved:
                return
            log.info('attempting to resolve zones %s', unresolved)
            resolver = ZoneResolver(self.api, self.alliance, set(self.config.country_ids))
            await resolver.resolve_all(self.config, self.zones)
            still = [z for z in self.config.zones if self.zones.get(z) is None]
            if still:
                log.warning("zones %s remain unresolved and will show as 'zone unresolved'. Run `python bot.py resolve --deep <min_lat> <max_lat> <min_lng> <max_lng>`, or add their IDs to known_region_ids in config.json.", still)

    def start_background_work(self) -> None:
        if self._startup is None or self._startup.done():
            self._startup = asyncio.create_task(self._startup_collection())
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._schedule_worker())
        self.reschedule_daily()

    async def _stop_background_work(self) -> None:
        tasks = [task for task in (self._startup, self._scheduler_task) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._startup = None
        self._scheduler_task = None

    def reschedule_daily(self) -> None:
        self._schedule_wakeup.set()

    async def _schedule_worker(self) -> None:
        while True:
            try:
                self._schedule_wakeup.clear()
                schedules = self.settings.list_schedules(enabled=True)
                if not schedules:
                    await self._schedule_wakeup.wait()
                    continue
                now = datetime.now(timezone.utc)
                next_run = min(_schedule_next_run(schedule, now) for schedule in schedules)
                timeout = max(0.0, (next_run - now).total_seconds())
                try:
                    await asyncio.wait_for(self._schedule_wakeup.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._run_due_schedules(next_run)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('report scheduler iteration failed; retrying in 10 seconds')
                await asyncio.sleep(10)

    async def _run_due_schedules(self, trigger_at: datetime) -> None:
        due = []
        for schedule in self.settings.list_schedules(enabled=True):
            occurrence = _schedule_next_run(schedule, trigger_at - timedelta(microseconds=1))
            if abs((occurrence - trigger_at).total_seconds()) < 1:
                due.append(schedule)
        if not due:
            return
        by_mode: dict[str, list[dict]] = defaultdict(list)
        for schedule in due:
            by_mode[schedule['mode']].append(schedule)
        for mode, schedules in by_mode.items():
            try:
                snapshot = await self.collect(mode)
            except Exception:
                log.exception('scheduled %s collection failed', mode)
                continue
            for schedule in schedules:
                try:
                    channel = await self.resolve_report_channel(schedule['channel_id'])
                    if channel is None:
                        raise RuntimeError('report channel was not found')
                    sent = await self.post_daily_sequence(channel, snapshot)
                    log.info('schedule %s posted (%d/%d zone messages)', schedule['id'], sent, len(self.config.zones))
                except Exception:
                    log.exception('schedule %s posting failed', schedule['id'])

    async def resolve_report_channel(self, channel_id: int):
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        return channel

    async def _startup_collection(self) -> None:
        try:
            snapshot = self.snapshots.latest(self.config.report_mode)
            if snapshot is None or snapshot_age_seconds(snapshot) > self.config.stale_snapshot_hours * 3600:
                log.info('no fresh snapshot; collecting on startup')
                await self.collect(self.config.report_mode)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('startup collection failed')

    async def post_daily_sequence(self, channel, snapshot: dict) -> int:
        sent = 0
        for index, zone in enumerate(self.config.zones):
            try:
                await channel.send(embed=build_zone_embed(snapshot, self.config, zone))
                sent += 1
            except Exception:
                log.exception('daily sequence: posting zone #%d failed', zone)
            if index < len(self.config.zones) - 1:
                await asyncio.sleep(5)
        return sent
