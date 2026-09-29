from __future__ import annotations
import asyncio
import base64
import json
import logging
import math
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timezone
import aiohttp
BACKEND = 'https://backend.wplace.live'
TILES_PER_AXIS = 2048
TILE_SIZE = 1000
DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
MODES = ('today', 'week', 'month', 'all-time')
log = logging.getLogger('wplace.api')

class WplaceError(Exception):
    pass

class WplaceAuthError(WplaceError):
    pass

class CloudflareChallenge(WplaceError):
    pass

def jwt_expiry(token: str) -> datetime | None:
    try:
        payload_b64 = token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get('exp')
        return datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
    except Exception:
        return None

def latlng_to_tile(lat: float, lng: float) -> tuple[int, int, int, int]:
    fx = (lng + 180.0) / 360.0 * TILES_PER_AXIS
    lat_rad = math.radians(lat)
    fy = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * TILES_PER_AXIS
    tx, ty = (int(fx), int(fy))
    px = min(TILE_SIZE - 1, max(0, int((fx - tx) * TILE_SIZE)))
    py = min(TILE_SIZE - 1, max(0, int((fy - ty) * TILE_SIZE)))
    return (tx, ty, px, py)

@dataclass
class Region:
    pass
    region_id: int
    number: int
    name: str
    city_id: int
    country_id: int

class WplaceClient:
    pass

    def __init__(self, session: aiohttp.ClientSession, j_cookie: str='', cf_clearance: str='', user_agent: str='', min_interval: float=0.3) -> None:
        self._session = session
        self._min_interval = min_interval
        self._throttle_lock = asyncio.Lock()
        self._last_request_at = 0.0
        ua = user_agent or DEFAULT_USER_AGENT
        self._headers = {'User-Agent': ua, 'Accept': 'application/json', 'Accept-Language': 'en-US,en;q=0.9', 'Referer': 'https://wplace.live/', 'Origin': 'https://wplace.live'}
        cookies = {}
        if j_cookie:
            cookies['j'] = j_cookie
        if cf_clearance:
            cookies['cf_clearance'] = cf_clearance
        self._cookie_jar = cookies

    async def get_json(self, path: str, *, attempts: int=4) -> object:
        url = BACKEND + path
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            async with self._throttle_lock:
                wait = self._last_request_at + self._min_interval - time_mod.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request_at = time_mod.monotonic()
            try:
                async with self._session.get(url, headers=self._headers, cookies=self._cookie_jar) as resp:
                    log.info('WPLACE_REQUEST path=%s status=%s', path, resp.status)
                    if resp.status == 429:
                        try:
                            retry_after = float(resp.headers.get('Retry-After') or 2 * attempt)
                        except ValueError:
                            retry_after = 2 * attempt
                        last_error = WplaceError(f'rate limited on {path}')
                        await asyncio.sleep(min(retry_after, 30))
                        continue
                    if resp.status in (401, 403):
                        body = await resp.text()
                        if resp.status == 403 and ('challenge' in body.lower() or 'just a moment' in body.lower()):
                            raise CloudflareChallenge('Cloudflare challenge. Set WPLACE_CF_CLEARANCE and WPLACE_USER_AGENT in .env.')
                        raise WplaceAuthError(f'{path} returned HTTP {resp.status}. A valid WPLACE_J cookie may be required.')
                    if resp.status >= 500:
                        last_error = WplaceError(f'{path} returned HTTP {resp.status}')
                        await asyncio.sleep(1.5 * attempt)
                        continue
                    if resp.status != 200:
                        raise WplaceError(f'{path} returned HTTP {resp.status}')
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and 'error' in data and ('status' in data):
                        status = data.get('status')
                        if status == 401:
                            raise WplaceAuthError(f"{path}: {data.get('error')}")
                        raise WplaceError(f"{path}: {data.get('error')} (status {status})")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(1.5 * attempt)
        raise WplaceError(f'{path} failed after {attempts} attempts: {last_error}')

    async def me(self) -> dict:
        return await self.get_json('/me')

    async def alliance(self) -> dict:
        return await self.get_json('/alliance')

    async def alliance_leaderboard(self, mode: str) -> list[dict]:
        self._check_mode(mode)
        return await self.get_json(f'/alliance/leaderboard/{mode}')

    async def region_leaderboard(self, mode: str, country_id: int) -> list[dict]:
        self._check_mode(mode)
        return await self.get_json(f'/leaderboard/region/{mode}/{country_id}')

    async def region_alliances(self, region_id: int, mode: str) -> list[dict]:
        self._check_mode(mode)
        return await self.get_json(f'/leaderboard/region/alliances/{region_id}/{mode}')

    async def region_players(self, region_id: int, mode: str) -> list[dict]:
        self._check_mode(mode)
        return await self.get_json(f'/leaderboard/region/players/{region_id}/{mode}')

    async def region_at(self, lat: float, lng: float) -> Region | None:
        tx, ty, px, py = latlng_to_tile(lat, lng)
        data = await self.get_json(f'/s0/pixel/{tx}/{ty}?x={px}&y={py}')
        if not isinstance(data, dict):
            return None
        region = data.get('region') or {}
        if not isinstance(region, dict) or 'id' not in region:
            return None
        try:
            return Region(region_id=int(region['id']), number=int(region.get('number', -1)), name=str(region.get('name', '')), city_id=int(region.get('cityId', 0)), country_id=int(region.get('countryId', 0)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _check_mode(mode: str) -> None:
        if mode not in MODES:
            raise WplaceError(f'unknown leaderboard mode {mode!r}; expected one of {MODES}')
