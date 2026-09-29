from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from .api import MODES, Region, WplaceClient, WplaceAuthError, WplaceError
from .config import Config
from .storage import ZoneRecord, ZoneStore
log = logging.getLogger('wplace.zones')

@dataclass
class AllianceIdentity:
    alliance_id: int | None
    name: str
    cookie_valid: bool | None

async def alliance_identity(api: WplaceClient, config: Config, j_cookie: str) -> AllianceIdentity:
    name = config.alliance_name
    if not j_cookie:
        return AllianceIdentity(None, name, None)
    try:
        me = await api.me()
    except WplaceAuthError:
        return AllianceIdentity(None, name, False)
    alliance_id = me.get('allianceId') or None
    try:
        alliance_id = int(alliance_id) if alliance_id is not None else None
    except (TypeError, ValueError):
        alliance_id = None
    try:
        info = await api.alliance()
        name = str(info.get('name', name))
    except WplaceAuthError:
        pass
    return AllianceIdentity(alliance_id, name, True)

class ZoneResolver:

    def __init__(self, api: WplaceClient, alliance: AllianceIdentity, preferred_country_ids: set[int] | None=None) -> None:
        self.api = api
        self.alliance = alliance
        self.preferred_country_ids = preferred_country_ids or set()

    async def resolve_all(self, config: Config, store: ZoneStore) -> dict[int, ZoneRecord | None]:
        for zone, region_id in config.known_region_ids.items():
            if store.get(zone) is None:
                store.put(Region(region_id=region_id, number=zone, name=config.known_region_names.get(zone, ''), city_id=0, country_id=0), zone, source='config')
        store.save()
        missing = [z for z in config.zones if store.get(z) is None]
        unnamed = [z for z in config.zones if store.get(z) is not None and (not store.get(z).name)]
        if not missing and (not unnamed):
            return {z: store.get(z) for z in config.zones}
        if missing:
            log.info('resolving %d zone(s) without a region ID: %s', len(missing), missing)
        candidates: dict[int, list[Region]] = {z: [] for z in missing}
        leaderboard_regions: dict[int, Region] = {}

        def add_candidate(region: Region, source: str) -> None:
            if region.number not in candidates:
                return
            existing = candidates[region.number]
            if all((r.region_id != region.region_id for r in existing)):
                existing.append(region)
                log.info('candidate %s: zone #%d id=%d (%s)', source, region.number, region.region_id, region.name)
        for country_id in config.country_ids:
            for mode in MODES:
                try:
                    rows = await self.api.region_leaderboard(mode, country_id)
                except WplaceError as exc:
                    log.warning('region leaderboard %s/%s failed: %s', mode, country_id, exc)
                    continue
                for row in rows:
                    region = _region_from_row(row)
                    if region:
                        leaderboard_regions[region.region_id] = region
                        add_candidate(region, f'country-{country_id}-leaderboard')
                await asyncio.sleep(0.2)
        for zone in unnamed:
            record = store.get(zone)
            known = leaderboard_regions.get(record.region_id)
            if known is not None:
                record.name = known.name
                record.city_id = known.city_id
                record.country_id = known.country_id
        if not missing:
            store.save()
            return {z: store.get(z) for z in config.zones}
        self._fill_region_names(store, candidates)
        if self.alliance.alliance_id is not None:
            for lat, lng, label in await self._footprint_coordinates():
                try:
                    region = await self.api.region_at(lat, lng)
                except WplaceError as exc:
                    log.warning('pixel probe at %.3f,%.3f failed: %s', lat, lng, exc)
                    continue
                if region:
                    add_candidate(region, f'footprint:{label}')
                await asyncio.sleep(0.2)
        resolved = await self._decide(config, store, candidates)
        store.save()
        return resolved

    async def deep_sweep(self, config: Config, store: ZoneStore, min_lat: float, max_lat: float, min_lng: float, max_lng: float, spacing: float=0.75) -> dict[int, ZoneRecord | None]:
        if spacing <= 0:
            raise ValueError('deep sweep spacing must be positive')
        if min_lat < max_lat:
            raise ValueError('deep sweep expects min_lat >= max_lat because latitude decreases northward')
        if min_lng > max_lng:
            raise ValueError('deep sweep expects min_lng <= max_lng')
        missing = [z for z in config.zones if store.get(z) is None]
        if not missing:
            return {z: store.get(z) for z in config.zones}
        wanted = set(missing)
        candidates: dict[int, list[Region]] = {z: [] for z in missing}
        total = int((min_lat - max_lat) / spacing) + 1
        done = 0
        lat = min_lat
        while lat >= max_lat:
            lng = min_lng
            while lng <= max_lng:
                try:
                    region = await self.api.region_at(lat, lng)
                except WplaceError:
                    region = None
                if region and region.number in wanted:
                    bucket = candidates[region.number]
                    if all((r.region_id != region.region_id for r in bucket)):
                        bucket.append(region)
                        log.info('deep sweep hit: zone #%d id=%d (%s) at %.2f,%.2f', region.number, region.region_id, region.name, lat, lng)
                lng += spacing
                await asyncio.sleep(0.05)
            lat -= spacing
            done += 1
            if done % 5 == 0 or done == total:
                log.info('deep sweep %.0f%% (%d/%d rows)', 100 * done / total, done, total)
        resolved = await self._decide(config, store, candidates)
        store.save()
        return resolved

    async def _decide(self, config: Config, store: ZoneStore, candidates: dict[int, list[Region]]) -> dict[int, ZoneRecord | None]:
        self._fill_region_names(store, candidates)
        resolved: dict[int, ZoneRecord | None] = {z: store.get(z) for z in config.zones}
        for zone, found in candidates.items():
            if not found:
                resolved[zone] = None
                log.warning('zone #%d could not be resolved automatically', zone)
                continue
            region = found[0] if len(found) == 1 else await self._disambiguate(found, zone)
            if region is not None:
                record = store.put(region, zone, source='auto')
                log.info('zone #%d resolved to region %d (%s)', zone, region.region_id, region.name or '?')
                resolved[zone] = record
        return resolved

    async def _disambiguate(self, found: list[Region], zone: int) -> Region | None:
        log.info('zone #%d has %d candidates across countries; verifying against the alliance', zone, len(found))
        for region in found:
            if await self._alliance_present(region.region_id):
                return region
        for region in found:
            if region.country_id in self.preferred_country_ids:
                return region
        return found[0]

    async def _alliance_present(self, region_id: int) -> bool:
        try:
            rows = await self.api.region_alliances(region_id, 'all-time')
        except WplaceError as exc:
            log.warning('alliance check for region %d failed: %s', region_id, exc)
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if self.alliance.alliance_id is not None:
                try:
                    if int(row.get('id', -1)) == self.alliance.alliance_id:
                        return True
                except (TypeError, ValueError):
                    pass
            if str(row.get('name', '')).casefold() == self.alliance.name.casefold():
                return True
        return False

    async def _footprint_coordinates(self) -> list[tuple[float, float, str]]:
        coords: list[tuple[float, float, str]] = []
        seen: set[tuple[float, float]] = set()

        def add(lat: float, lng: float, label: str) -> None:
            key = (round(lat, 2), round(lng, 2))
            if key not in seen:
                seen.add(key)
                coords.append((lat, lng, label))
        for mode in MODES:
            try:
                rows = await self.api.alliance_leaderboard(mode)
            except WplaceError as exc:
                log.warning('alliance leaderboard %s failed: %s', mode, exc)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                lat, lng = (row.get('lastLatitude'), row.get('lastLongitude'))
                if lat is not None and lng is not None:
                    try:
                        add(float(lat), float(lng), f'member-{mode}')
                    except (TypeError, ValueError):
                        continue
        try:
            info = await self.api.alliance()
            hq = info.get('hq') or {}
            if hq.get('latitude') is not None and hq.get('longitude') is not None:
                add(float(hq['latitude']), float(hq['longitude']), 'hq')
        except WplaceError as exc:
            log.warning('alliance info failed: %s', exc)
        try:
            me = await self.api.me()
            for fav in me.get('favoriteLocations', []):
                if not isinstance(fav, dict):
                    continue
                if fav.get('latitude') is not None and fav.get('longitude') is not None:
                    try:
                        add(float(fav['latitude']), float(fav['longitude']), 'favorite')
                    except (TypeError, ValueError):
                        continue
        except WplaceError as exc:
            log.warning('favorites lookup failed: %s', exc)
        return coords

    def _fill_region_names(self, store: ZoneStore, candidates: dict[int, list[Region]]) -> None:
        for zone, found in candidates.items():
            record = store.get(zone)
            if record is not None and (not record.name) and found:
                match = next((r for r in found if r.region_id == record.region_id), None)
                if match:
                    record.name = match.name
                    record.city_id = match.city_id
                    record.country_id = match.country_id

def _region_from_row(row: dict) -> Region | None:
    try:
        return Region(region_id=int(row['id']), number=int(row['number']), name=str(row.get('name', '')), city_id=int(row.get('cityId', 0)), country_id=int(row.get('countryId', 0)))
    except (KeyError, TypeError, ValueError):
        return None
