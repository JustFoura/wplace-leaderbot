from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path
import aiohttp
from wplace_lb.api import WplaceClient
from wplace_lb.collector import collect_and_store
from wplace_lb.config import Config, ConfigError, Secrets
from wplace_lb.countries import country_id_for_code
from wplace_lb.render import format_report
from wplace_lb.storage import SnapshotStore, ZoneStore
from wplace_lb.zones import ZoneResolver, alliance_identity
log = logging.getLogger('wplace')
ROOT = Path(__file__).resolve().parent

def load_setup() -> tuple[Config, Secrets]:
    config = Config.load(ROOT / 'config.json')
    if not config.data_dir.is_absolute():
        config.data_dir = ROOT / config.data_dir
    secrets = Secrets.load(ROOT / '.env')
    return (config, secrets)

async def make_client(secrets: Secrets) -> tuple[aiohttp.ClientSession, WplaceClient]:
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    api = WplaceClient(session, j_cookie=secrets.wplace_j, cf_clearance=secrets.wplace_cf_clearance, user_agent=secrets.wplace_user_agent)
    return (session, api)

async def cmd_run(config: Config, secrets: Secrets) -> int:
    from wplace_lb.discord_bot import WplaceLeaderboardBot
    if not secrets.discord_token:
        log.error('DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.')
        return 2
    bot = WplaceLeaderboardBot(config, secrets)
    await bot.start()
    return 0

async def cmd_resolve(config: Config, secrets: Secrets, args: argparse.Namespace) -> int:
    session, api = await make_client(secrets)
    try:
        alliance = await alliance_identity(api, config, secrets.wplace_j)
        if alliance.cookie_valid is False:
            log.warning('WPLACE_J rejected by /me; continuing anonymously')
        log.info('alliance: %s (id=%s)', alliance.name, alliance.alliance_id)
        preferred = set(config.country_ids)
        if secrets.wplace_j:
            try:
                me = await api.me()
                cid = country_id_for_code(str(me.get('country', '')))
                if cid:
                    preferred.add(cid)
            except Exception:
                pass
        store = ZoneStore(config.data_dir / 'zone_ids.json')
        resolver = ZoneResolver(api, alliance, preferred)
        if args.deep:
            deep_args = args.deep
            min_lat = float(deep_args[0]) if len(deep_args) > 0 else -44.0
            max_lat = float(deep_args[1]) if len(deep_args) > 1 else -10.0
            min_lng = float(deep_args[2]) if len(deep_args) > 2 else 112.0
            max_lng = float(deep_args[3]) if len(deep_args) > 3 else 154.0
            spacing = float(deep_args[4]) if len(deep_args) > 4 else 0.75
            log.info('deep sweep: lat %.2f..%.2f lng %.2f..%.2f spacing %.2f — this can take a while', min_lat, max_lat, min_lng, max_lng, spacing)
            try:
                resolved = await resolver.deep_sweep(config, store, min_lat, max_lat, min_lng, max_lng, spacing)
            except ValueError as exc:
                log.error('invalid deep sweep arguments: %s', exc)
                return 2
        else:
            resolved = await resolver.resolve_all(config, store)
        print()
        print('Zone      Region ID   Name             Status')
        missing = []
        for zone in config.zones:
            record = resolved.get(zone)
            if record is None:
                missing.append(zone)
                print(f'#{zone:<7} —           —                UNRESOLVED')
            else:
                print(f"#{zone:<7} {record.region_id:<11} {record.name or '?':<16} ok ({record.source})")
        if missing:
            print(f'\n{len(missing)} zone(s) unresolved. Options:\n  • run: python bot.py resolve --deep [min_lat max_lat min_lng max_lng [spacing]]\n  • or add their IDs to known_region_ids in config.json')
            return 1
        return 0
    finally:
        await session.close()

async def cmd_collect(config: Config, secrets: Secrets, mode: str) -> int:
    session, api = await make_client(secrets)
    try:
        alliance = await alliance_identity(api, config, secrets.wplace_j)
        if alliance.cookie_valid is False:
            log.warning('WPLACE_J rejected by /me; matching alliance by name only')
        store = ZoneStore(config.data_dir / 'zone_ids.json')
        snapshots = SnapshotStore(config.data_dir / 'snapshots.json')
        resolver = ZoneResolver(api, alliance, set(config.country_ids) | await _me_country(api, secrets))
        await resolver.resolve_all(config, store)
        snapshot = await collect_and_store(api, config, store, snapshots, mode, alliance.alliance_id, alliance.name)
        print(format_report(snapshot, config))
        return 0
    finally:
        await session.close()

async def _me_country(api: WplaceClient, secrets: Secrets) -> set[int]:
    if not secrets.wplace_j:
        return set()
    try:
        me = await api.me()
        cid = country_id_for_code(str(me.get('country', '')))
        return {cid} if cid else set()
    except Exception:
        return set()

async def cmd_show(config: Config, mode: str) -> int:
    snapshots = SnapshotStore(config.data_dir / 'snapshots.json')
    snapshot = snapshots.latest(mode)
    if snapshot is None:
        print(f'No snapshot stored for mode {mode!r}. Run: python bot.py collect --mode {mode}')
        return 1
    print(format_report(snapshot, config))
    return 0

def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('run', help='run the Discord bot (default)')
    p_resolve = sub.add_parser('resolve', help='resolve zone numbers to region IDs')
    p_resolve.add_argument('--deep', nargs='*', help='exhaustive grid sweep; optional bbox: min_lat max_lat min_lng max_lng [spacing]')
    p_collect = sub.add_parser('collect', help='collect a snapshot now and print it')
    p_collect.add_argument('--mode', default=None, help='today | week | month | all-time')
    p_show = sub.add_parser('show', help='print the latest stored snapshot')
    p_show.add_argument('--mode', default=None, help='today | week | month | all-time')
    args = parser.parse_args()
    try:
        config, secrets = load_setup()
    except ConfigError as exc:
        log.error('%s', exc)
        return 2
    command = args.command or 'run'
    if command == 'run':
        return asyncio.run(cmd_run(config, secrets))
    if command == 'resolve':
        return asyncio.run(cmd_resolve(config, secrets, args))
    if command == 'collect':
        return asyncio.run(cmd_collect(config, secrets, args.mode or config.default_mode))
    if command == 'show':
        return asyncio.run(cmd_show(config, args.mode or config.default_mode))
    parser.error(f'unknown command {command!r}')
    return 2
if __name__ == '__main__':
    sys.exit(main())
