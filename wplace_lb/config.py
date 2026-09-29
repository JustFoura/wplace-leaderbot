from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

class ConfigError(Exception):
    pass

def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values

@dataclass
class Secrets:
    discord_token: str = ''
    discord_channel_id: int | None = None
    discord_guild_id: int | None = None
    discord_guild_ids: tuple[int, ...] = ()
    wplace_j: str = ''
    wplace_cf_clearance: str = ''
    wplace_user_agent: str = ''

    @classmethod
    def load(cls, env_path: Path) -> 'Secrets':
        env = load_env(env_path)

        def get(key: str) -> str:
            import os
            return os.environ.get(key) or env.get(key, '')

        def get_int(key: str) -> int | None:
            raw = get(key).strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f'{key} must be an integer ID, got {raw!r}') from exc

        guilds_raw = get('DISCORD_GUILD_IDS').strip() or get('DISCORD_GUILD_ID').strip()
        guild_ids: list[int] = []
        for value in guilds_raw.replace(',', ' ').split():
            try:
                guild_id = int(value)
            except ValueError as exc:
                raise ConfigError(f'DISCORD_GUILD_IDS must contain integer IDs, got {value!r}') from exc
            if guild_id < 1:
                raise ConfigError(f'DISCORD_GUILD_IDS must contain positive IDs, got {guild_id}')
            if guild_id not in guild_ids:
                guild_ids.append(guild_id)
        return cls(discord_token=get('DISCORD_TOKEN'), discord_channel_id=get_int('DISCORD_CHANNEL_ID'), discord_guild_id=guild_ids[0] if guild_ids else None, discord_guild_ids=tuple(guild_ids), wplace_j=get('WPLACE_J'), wplace_cf_clearance=get('WPLACE_CF_CLEARANCE'), wplace_user_agent=get('WPLACE_USER_AGENT'))

@dataclass
class Config:
    alliance_name: str
    zones: list[int]
    known_region_ids: dict[int, int] = field(default_factory=dict)
    known_region_names: dict[int, str] = field(default_factory=dict)
    country_ids: list[int] = field(default_factory=lambda: [13])
    timezone: str = 'UTC'
    daily_report_time: str = '23:45'
    report_mode: str = 'today'
    default_mode: str = 'today'
    stale_snapshot_hours: float = 6.0
    data_dir: Path = Path('data')

    @classmethod
    def load(cls, path: Path) -> 'Config':
        if not path.exists():
            raise ConfigError(f'Missing config file: {path}')
        raw = json.loads(path.read_text(encoding='utf-8'))
        try:
            alliance_name = str(raw['alliance_name']).strip()
            zones = [int(z) for z in raw['zones']]
        except KeyError as exc:
            raise ConfigError(f'config.json is missing required key {exc}') from exc
        if not alliance_name:
            raise ConfigError('config.json: alliance_name must not be empty')
        if not zones:
            raise ConfigError('config.json: zones must list at least one zone number')
        known_ids: dict[int, int] = {}
        known_names: dict[int, str] = {}
        for key, value in raw.get('known_region_ids', {}).items():
            zone = int(key)
            if isinstance(value, dict):
                known_ids[zone] = int(value['id'])
                if value.get('name'):
                    known_names[zone] = str(value['name'])
            else:
                known_ids[zone] = int(value)
        country_ids = [int(c) for c in raw.get('country_ids', [13])]
        cfg = cls(alliance_name=alliance_name, zones=zones, known_region_ids=known_ids, known_region_names=known_names, country_ids=country_ids, timezone=str(raw.get('timezone', 'UTC')), daily_report_time=str(raw.get('daily_report_time', '23:45')), report_mode=str(raw.get('report_mode', 'today')), default_mode=str(raw.get('default_mode', 'today')), stale_snapshot_hours=float(raw.get('stale_snapshot_hours', 6)), data_dir=Path(str(raw.get('data_dir', 'data'))))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ConfigError(f'config.json: unknown timezone {self.timezone!r}') from exc
        try:
            self.report_schedule()
        except ValueError as exc:
            raise ConfigError(f'config.json: daily_report_time {self.daily_report_time!r}: {exc}') from exc
        for mode in (self.report_mode, self.default_mode):
            if mode not in VALID_MODES:
                raise ConfigError(f'config.json: mode {mode!r} not in {sorted(VALID_MODES)}')
        if self.stale_snapshot_hours <= 0:
            raise ConfigError('config.json: stale_snapshot_hours must be positive')

    def report_schedule(self) -> dt_time:
        hour_s, _, minute_s = self.daily_report_time.partition(':')
        hour, minute = (int(hour_s), int(minute_s or 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError('expected HH:MM with hour 0-23 and minute 0-59')
        return dt_time(hour, minute, tzinfo=ZoneInfo(self.timezone))

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
VALID_MODES = {'today', 'week', 'month', 'all-time'}
