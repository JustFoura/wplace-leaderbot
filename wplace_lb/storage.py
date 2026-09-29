from __future__ import annotations
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .api import MODES, Region

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

@dataclass
class ZoneRecord:
    zone: int
    region_id: int
    number: int
    name: str
    city_id: int
    country_id: int
    resolved_at: str
    source: str

class ZoneStore:

    def __init__(self, path: Path) -> None:
        self.path = path
        self._zones: dict[int, ZoneRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or not isinstance(raw.get('zones'), list):
            return
        for entry in raw['zones']:
            try:
                record = ZoneRecord(zone=int(entry['zone']), region_id=int(entry['region_id']), number=int(entry['number']), name=str(entry.get('name', '')), city_id=int(entry.get('city_id', 0)), country_id=int(entry.get('country_id', 0)), resolved_at=str(entry.get('resolved_at', '')), source=str(entry.get('source', '')))
            except (KeyError, TypeError, ValueError):
                continue
            self._zones[record.zone] = record

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'zones': [asdict(self._zones[z]) for z in sorted(self._zones)]}
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)

    def get(self, zone: int) -> ZoneRecord | None:
        return self._zones.get(zone)

    def put(self, region: Region, zone: int, source: str) -> ZoneRecord:
        record = ZoneRecord(zone=zone, region_id=region.region_id, number=region.number, name=region.name, city_id=region.city_id, country_id=region.country_id, resolved_at=utc_now_iso(), source=source)
        self._zones[zone] = record
        return record

    def all_zones(self) -> dict[int, ZoneRecord]:
        return dict(self._zones)

@dataclass
class ZoneSnapshot:
    zone: int
    region_id: int
    region_name: str
    present: bool
    rank: int | None
    pixels: int | None
    error: str | None = None

class SnapshotStore:
    KEEP_DAYS = 90
    MAX_SNAPSHOTS = 10_000

    def __init__(self, path: Path, max_snapshots: int | None=None) -> None:
        self.path = path
        self.max_snapshots = self.MAX_SNAPSHOTS if max_snapshots is None else max_snapshots
        if self.max_snapshots < 1:
            raise ValueError('max_snapshots must be positive')
        self._snapshots: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict) and isinstance(raw.get('snapshots'), list):
            self._snapshots = [item for item in raw['snapshots'] if isinstance(item, dict)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'snapshots': self._snapshots}
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)

    def append(self, snapshot: dict) -> None:
        self._snapshots.append(snapshot)
        self._prune()
        self.save()

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - self.KEEP_DAYS * 86400
        kept = []
        for snap in self._snapshots:
            try:
                ts = datetime.fromisoformat(snap['collected_at']).timestamp()
            except (KeyError, ValueError, TypeError):
                kept.append(snap)
                continue
            if ts >= cutoff:
                kept.append(snap)
        self._snapshots = kept[-self.max_snapshots:]

    def latest(self, mode: str) -> dict | None:
        for snap in reversed(self._snapshots):
            if snap.get('mode') == mode:
                return snap
        return None

    def previous(self, mode: str) -> dict | None:
        matches = [snap for snap in self._snapshots if snap.get('mode') == mode]
        return matches[-2] if len(matches) >= 2 else None

class SettingsStore:
    """Persistent report settings with compatibility for the original format.

    Version one stored one global channel and timer as top-level keys. Version
    two stores independent schedules, each scoped to a Discord guild. The
    legacy values are retained until a bot instance has enough guild context to
    migrate them safely.
    """

    KEYS = ('report_time', 'timezone', 'report_mode', 'channel_id')
    SCHEDULE_KEYS = ('id', 'guild_id', 'channel_id', 'name', 'time', 'timezone', 'mode', 'enabled')

    def __init__(self, path: Path) -> None:
        self.path = path
        self._values: dict = {}
        self._schedules: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        legacy = raw.get('legacy') if isinstance(raw.get('legacy'), dict) else raw
        self._values = {k: legacy[k] for k in self.KEYS if k in legacy}
        raw_schedules = raw.get('schedules', [])
        if isinstance(raw_schedules, list):
            self._schedules = [schedule for item in raw_schedules if (schedule := self._normalise_schedule(item)) is not None]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': 2, 'schedules': self._schedules}
        if self._values:
            payload['legacy'] = self._values
        temporary = self.path.with_name(f'.{self.path.name}.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        temporary.replace(self.path)

    def get(self, key: str):
        return self._values.get(key)

    def update(self, values: dict) -> None:
        for key, value in values.items():
            if key not in self.KEYS:
                raise ValueError(f'unknown setting {key!r}')
            if value is None:
                self._values.pop(key, None)
            else:
                self._values[key] = value
        self.save()

    @staticmethod
    def _valid_time(value: object) -> bool:
        if not isinstance(value, str):
            return False
        parts = value.split(':')
        if len(parts) != 2:
            return False
        try:
            hour, minute = (int(part) for part in parts)
        except ValueError:
            return False
        return 0 <= hour <= 23 and 0 <= minute <= 59

    @classmethod
    def _normalise_schedule(cls, raw: object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        try:
            schedule_id = str(raw['id']).strip()
            guild_id = int(raw['guild_id'])
            channel_id = int(raw['channel_id'])
            name = str(raw.get('name') or 'Daily').strip()[:50]
            report_time = str(raw['time']).strip()
            timezone = str(raw['timezone']).strip()
            mode = str(raw['mode']).strip()
        except (KeyError, TypeError, ValueError):
            return None
        if not schedule_id or guild_id < 1 or channel_id < 1 or not name:
            return None
        if not cls._valid_time(report_time) or mode not in MODES:
            return None
        try:
            ZoneInfo(timezone)
        except Exception:
            return None
        return {
            'id': schedule_id,
            'guild_id': guild_id,
            'channel_id': channel_id,
            'name': name,
            'time': report_time,
            'timezone': timezone,
            'mode': mode,
            'enabled': bool(raw.get('enabled', True)),
        }

    def list_schedules(self, guild_id: int | None=None, *, enabled: bool | None=None) -> list[dict]:
        schedules = [schedule for schedule in self._schedules if guild_id is None or schedule['guild_id'] == guild_id]
        if enabled is not None:
            schedules = [schedule for schedule in schedules if schedule['enabled'] is enabled]
        return [dict(schedule) for schedule in sorted(schedules, key=lambda item: (item['guild_id'], item['time'], item['name'].casefold(), item['id']))]

    def get_schedule(self, schedule_id: str, guild_id: int | None=None) -> dict | None:
        for schedule in self._schedules:
            if schedule['id'] == schedule_id and (guild_id is None or schedule['guild_id'] == guild_id):
                return dict(schedule)
        return None

    def add_schedule(self, *, guild_id: int, channel_id: int, name: str, report_time: str, timezone: str, mode: str, enabled: bool=True) -> dict:
        name = name.strip()[:50] or 'Daily'
        candidate = {
            'id': uuid.uuid4().hex[:8],
            'guild_id': int(guild_id),
            'channel_id': int(channel_id),
            'name': name,
            'time': report_time,
            'timezone': timezone,
            'mode': mode,
            'enabled': bool(enabled),
        }
        normalised = self._normalise_schedule(candidate)
        if normalised is None:
            raise ValueError('invalid report schedule')
        while self.get_schedule(normalised['id']) is not None:
            normalised['id'] = uuid.uuid4().hex[:8]
        self._schedules.append(normalised)
        self.save()
        return dict(normalised)

    def update_schedule(self, schedule_id: str, guild_id: int, updates: dict) -> dict | None:
        for index, current in enumerate(self._schedules):
            if current['id'] != schedule_id or current['guild_id'] != guild_id:
                continue
            candidate = dict(current)
            candidate.update(updates)
            normalised = self._normalise_schedule(candidate)
            if normalised is None:
                raise ValueError('invalid report schedule update')
            self._schedules[index] = normalised
            self.save()
            return dict(normalised)
        return None

    def remove_schedule(self, schedule_id: str, guild_id: int) -> bool:
        original_count = len(self._schedules)
        self._schedules = [schedule for schedule in self._schedules if not (schedule['id'] == schedule_id and schedule['guild_id'] == guild_id)]
        if len(self._schedules) == original_count:
            return False
        self.save()
        return True

    def migrate_legacy(self, *, guild_id: int, fallback_channel_id: int | None, default_time: str, default_timezone: str, default_mode: str) -> dict | None:
        if self.list_schedules(guild_id):
            return None
        channel_value = self._values.get('channel_id') or fallback_channel_id
        if channel_value is None:
            return None
        try:
            channel_id = int(channel_value)
        except (TypeError, ValueError):
            return None
        if channel_id < 1:
            return None
        report_time = str(self._values.get('report_time') or default_time)
        if not self._valid_time(report_time):
            report_time = default_time
        timezone = str(self._values.get('timezone') or default_timezone)
        try:
            ZoneInfo(timezone)
        except Exception:
            timezone = default_timezone
        mode = str(self._values.get('report_mode') or default_mode)
        if mode not in MODES:
            mode = default_mode
        schedule = self.add_schedule(
            guild_id=guild_id,
            channel_id=channel_id,
            name='Daily',
            report_time=report_time,
            timezone=timezone,
            mode=mode,
        )
        return schedule

    def as_display(self) -> dict:
        return dict(self._values)
