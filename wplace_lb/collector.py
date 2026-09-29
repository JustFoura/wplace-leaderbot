from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from .api import WplaceClient, WplaceError
from .config import Config
from .storage import SnapshotStore, ZoneStore, utc_now_iso
log = logging.getLogger('wplace.collect')

def _int_value(value: object, default: int=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def find_alliance_row(rows: list[dict], alliance_id: int | None, alliance_name: str) -> tuple[int, dict] | None:
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        row_id = row.get('id')
        if alliance_id is not None and row_id is not None and _int_value(row_id, -1) == alliance_id:
            return (index + 1, row)
        if str(row.get('name', '')).casefold() == alliance_name.casefold():
            return (index + 1, row)
    return None

def parse_player_row(index: int, row: dict) -> dict:
    return {'rank': index + 1, 'player_id': _int_value(row.get('id')), 'name': str(row.get('name', '')), 'alliance_id': _int_value(row.get('allianceId')), 'alliance_name': str(row.get('allianceName', '')), 'pixels': _int_value(row.get('pixelsPainted'))}

def parse_alliance_rows(rows: list[dict], limit: int=5) -> list[dict]:
    parsed = []
    for index, row in enumerate(rows[:limit]):
        if not isinstance(row, dict):
            continue
        parsed.append({'rank': index + 1, 'id': _int_value(row.get('id')), 'name': str(row.get('name', '')), 'pixels': _int_value(row.get('pixelsPainted'))})
    return parsed

def alliance_members(players: list[dict], alliance_id: int | None, alliance_name: str) -> list[dict]:
    target = alliance_name.casefold()
    return [p for p in players if alliance_id is not None and p['alliance_id'] == alliance_id or (p['alliance_name'] and p['alliance_name'].casefold() == target)]

async def collect_snapshot(api: WplaceClient, config: Config, store: ZoneStore, mode: str, alliance_id: int | None, alliance_name: str, include_players: bool=True) -> dict:
    zones_payload: dict[str, dict] = {}
    for zone in config.zones:
        record = store.get(zone)
        if record is None:
            zones_payload[str(zone)] = {'region_id': None, 'region_name': '', 'present': False, 'rank': None, 'pixels': None, 'error': 'zone unresolved'}
            continue
        entry: dict = {'region_id': record.region_id, 'region_name': record.name, 'present': False, 'rank': None, 'pixels': None, 'error': None}
        zones_payload[str(zone)] = entry
        try:
            rows = await api.region_alliances(record.region_id, mode)
            entry['alliances'] = parse_alliance_rows(rows)
            match = find_alliance_row(rows, alliance_id, alliance_name)
            if match:
                rank, row = match
                entry['present'] = True
                entry['rank'] = rank
                entry['pixels'] = _int_value(row.get('pixelsPainted'))
        except WplaceError as exc:
            log.warning('zone #%d (region %d) failed: %s', zone, record.region_id, exc)
            entry['error'] = str(exc)
            await asyncio.sleep(0.25)
            continue
        if include_players:
            try:
                player_rows = await api.region_players(record.region_id, mode)
                entry['players'] = [parse_player_row(i, row) for i, row in enumerate(player_rows[:10]) if isinstance(row, dict)]
            except WplaceError as exc:
                log.warning('zone #%d players board failed: %s', zone, exc)
        await asyncio.sleep(0.25)
    return {'collected_at': utc_now_iso(), 'mode': mode, 'alliance': {'id': alliance_id, 'name': alliance_name}, 'zones': zones_payload}

async def collect_and_store(api: WplaceClient, config: Config, store: ZoneStore, snapshots: SnapshotStore, mode: str, alliance_id: int | None, alliance_name: str, include_players: bool=True) -> dict:
    snapshot = await collect_snapshot(api, config, store, mode, alliance_id, alliance_name, include_players)
    snapshots.append(snapshot)
    missing = [z for z in config.zones if store.get(z) is None]
    failed = [z for z, v in snapshot['zones'].items() if v.get('error')]
    if missing:
        log.warning('snapshot incomplete: unresolved zones %s', missing)
    if failed:
        log.warning('snapshot incomplete: request failures for zones %s', failed)
    return snapshot

def snapshot_age_seconds(snapshot: dict) -> float:
    collected = datetime.fromisoformat(snapshot['collected_at'])
    return (datetime.now(timezone.utc) - collected).total_seconds()
