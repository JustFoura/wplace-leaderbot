from __future__ import annotations
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wplace_lb.api import Region, latlng_to_tile
from wplace_lb.collector import alliance_members, collect_snapshot, find_alliance_row, parse_alliance_rows, parse_player_row
from wplace_lb.config import Config, Secrets
from wplace_lb.discord_bot import WplaceLeaderboardBot, _schedule_next_run
from wplace_lb.render import build_embed, build_zone_embed, format_report
from wplace_lb.storage import SettingsStore, SnapshotStore, ZoneStore
from wplace_lb.zones import AllianceIdentity, ZoneResolver
EXAMPLE_ID = 9001
EXAMPLE_NAME = 'Example Alliance'

def test_tile_math() -> None:
    assert latlng_to_tile(0.0, 0.0) == (1024, 1024, 0, 0)
    tx, ty, px, py = latlng_to_tile(22.535, 114.011529)
    assert (tx, ty) == (1672, 892), (tx, ty)
    for lat, lng in [(85.0, 179.9), (-85.0, -179.9), (-31.95, 115.85), (51.5, -0.12)]:
        _, _, px, py = latlng_to_tile(lat, lng)
        assert 0 <= px <= 999 and 0 <= py <= 999, (lat, lng, px, py)

def test_find_alliance_row() -> None:
    rows = [{'id': 1, 'name': 'Other Alliance', 'pixelsPainted': 100}, {'id': EXAMPLE_ID, 'name': EXAMPLE_NAME.lower(), 'pixelsPainted': 50}]
    assert find_alliance_row(rows, EXAMPLE_ID, EXAMPLE_NAME) == (2, rows[1])
    assert find_alliance_row(rows, None, EXAMPLE_NAME) == (2, rows[1])
    assert find_alliance_row(rows[:1], EXAMPLE_ID, EXAMPLE_NAME) is None

def test_malformed_api_rows_are_ignored_or_defaulted() -> None:
    assert find_alliance_row([{'id': 'not-an-id', 'name': 'other'}, None], EXAMPLE_ID, EXAMPLE_NAME) is None
    assert parse_alliance_rows([{'id': 'bad', 'pixelsPainted': 'bad', 'name': 'other'}, None]) == [{'rank': 1, 'id': 0, 'name': 'other', 'pixels': 0}]
    assert parse_player_row(0, {'id': 'bad', 'allianceId': None, 'pixelsPainted': 'bad'})['pixels'] == 0

class FakeAPI:

    def __init__(self) -> None:
        self.example_rows = [{'id': 7001, 'name': 'Example City', 'cityId': 70, 'number': 101, 'countryId': 1}, {'id': 7002, 'name': 'Example City', 'cityId': 70, 'number': 202, 'countryId': 1}]
        self.collision_rows = [{'id': 9002, 'name': 'Other City', 'cityId': 90, 'number': 202, 'countryId': 99}]
        self.alliance_boards = {7001: [{'id': EXAMPLE_ID, 'name': EXAMPLE_NAME, 'pixelsPainted': 2413}, {'id': 1, 'name': 'Other Alliance', 'pixelsPainted': 962}], 7002: [{'id': 1, 'name': 'Other Alliance', 'pixelsPainted': 962}], 9002: [{'id': EXAMPLE_ID, 'name': EXAMPLE_NAME, 'pixelsPainted': 7}]}
        self.player_boards = {7001: [{'id': 3001, 'name': 'ExamplePainter', 'allianceId': EXAMPLE_ID, 'allianceName': EXAMPLE_NAME, 'pixelsPainted': 1353}, {'id': 3002, 'name': 'ExampleMember', 'allianceId': EXAMPLE_ID, 'allianceName': EXAMPLE_NAME, 'pixelsPainted': 639}, {'id': 555, 'name': 'RivalPainter', 'allianceId': 1, 'allianceName': 'Other Alliance', 'pixelsPainted': 100}], 7002: [{'id': 555, 'name': 'RivalPainter', 'allianceId': 1, 'allianceName': 'Other Alliance', 'pixelsPainted': 962}]}

    async def region_leaderboard(self, mode: str, country_id: int) -> list[dict]:
        if country_id == 1:
            return self.example_rows
        if country_id == 99:
            return self.collision_rows
        return []

    async def region_alliances(self, region_id: int, mode: str) -> list[dict]:
        return self.alliance_boards.get(region_id, [])

    async def region_players(self, region_id: int, mode: str) -> list[dict]:
        return self.player_boards.get(region_id, [])

    async def region_at(self, lat: float, lng: float) -> Region | None:
        return None

    async def me(self) -> dict:
        return {'allianceId': EXAMPLE_ID, 'country': 'XX'}

    async def alliance(self) -> dict:
        return {'id': EXAMPLE_ID, 'name': EXAMPLE_NAME, 'hq': {'latitude': 0.0, 'longitude': 0.0}}

    async def alliance_leaderboard(self, mode: str) -> list[dict]:
        return []

def make_config(tmp: Path, **overrides) -> Config:
    raw = {'alliance_name': EXAMPLE_NAME, 'zones': [101, 202], 'known_region_ids': {}, 'country_ids': [1], 'data_dir': str(tmp / 'data')}
    raw.update(overrides)
    import json
    path = tmp / 'config.json'
    path.write_text(json.dumps(raw))
    return Config.load(path)

def test_resolver_resolves_from_leaderboards() -> None:

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            config = make_config(tmp)
            store = ZoneStore(tmp / 'zone_ids.json')
            api = FakeAPI()
            resolver = ZoneResolver(api, AllianceIdentity(EXAMPLE_ID, EXAMPLE_NAME, True), {1})
            resolved = await resolver.resolve_all(config, store)
            assert resolved[101] is not None and resolved[101].region_id == 7001
            assert resolved[202] is not None and resolved[202].region_id == 7002
            assert store.get(101).name == 'Example City'
            store2 = ZoneStore(tmp / 'zone_ids.json')
            assert store2.get(101).region_id == 7001
    asyncio.run(run())

def test_resolver_disambiguates_by_alliance_presence() -> None:

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            config = make_config(tmp, country_ids=[1, 99])
            store = ZoneStore(tmp / 'zone_ids.json')
            api = FakeAPI()
            resolver = ZoneResolver(api, AllianceIdentity(EXAMPLE_ID, EXAMPLE_NAME, True), {1})
            resolved = await resolver.resolve_all(config, store)
            assert resolved[202].region_id == 9002
    asyncio.run(run())

def test_resolver_reports_unresolved() -> None:

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            config = make_config(tmp, zones=[101, 303])
            store = ZoneStore(tmp / 'zone_ids.json')
            api = FakeAPI()
            resolver = ZoneResolver(api, AllianceIdentity(None, EXAMPLE_NAME, None), {1})
            resolved = await resolver.resolve_all(config, store)
            assert resolved[101] is not None
            assert resolved[303] is None
    asyncio.run(run())

def test_deep_sweep_rejects_invalid_bounds() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            config = make_config(tmp)
            store = ZoneStore(tmp / 'zone_ids.json')
            resolver = ZoneResolver(FakeAPI(), AllianceIdentity(None, EXAMPLE_NAME, None), {1})
            for args in ((-10.0, -20.0, 112.0, 154.0, 0.0), (-20.0, -10.0, 112.0, 154.0, 0.75), (-20.0, -10.0, 154.0, 112.0, 0.75)):
                try:
                    await resolver.deep_sweep(config, store, *args)
                except ValueError:
                    continue
                raise AssertionError(f'invalid deep sweep arguments were accepted: {args}')
    asyncio.run(run())

def test_runtime_stores_ignore_malformed_json() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        for filename, store_type in (('zone_ids.json', ZoneStore), ('snapshots.json', SnapshotStore), ('settings.json', SettingsStore)):
            path = tmp / filename
            path.write_text('{not valid json', encoding='utf-8')
            store = store_type(path)
            if isinstance(store, ZoneStore):
                assert store.all_zones() == {}
            elif isinstance(store, SnapshotStore):
                assert store.latest('today') is None
            else:
                assert store.list_schedules() == []

def test_collector_snapshot() -> None:

    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            config = make_config(tmp)
            store = ZoneStore(tmp / 'zone_ids.json')
            store.put(Region(7001, 101, 'Example City', 70, 1), 101, 'test')
            store.put(Region(7002, 202, 'Example City', 70, 1), 202, 'test')
            snapshot = await collect_snapshot(FakeAPI(), config, store, 'today', EXAMPLE_ID, EXAMPLE_NAME)
            entry = snapshot['zones']['101']
            assert entry['present'] is True and entry['pixels'] == 2413
            assert entry['alliances'][0]['name'] == EXAMPLE_NAME
            assert entry['players'][0]['player_id'] == 3001
            assert snapshot['zones']['202']['present'] is False
            assert snapshot['zones']['202']['pixels'] is None
            members = alliance_members(entry['players'], EXAMPLE_ID, EXAMPLE_NAME)
            assert [m['name'] for m in members] == ['ExamplePainter', 'ExampleMember']
            text = format_report(snapshot, config)
            assert 'not ranked' in text and '2,413' in text
            overview = build_embed(snapshot, config)
            assert 'not ranked' in [f.value for f in overview.fields]
            zone = build_zone_embed(snapshot, config, 101)
            assert zone.title.startswith('Example City #101')
            alliance_field = next((f for f in zone.fields if f.name == 'Alliances'))
            assert '**1 🥇. Example Alliance — 2,413** ✅' in alliance_field.value
            players_field = next((f for f in zone.fields if f.name == 'Top painters'))
            assert '**1 🥇. ExamplePainter #3001 — 1,353** ✅' in players_field.value
            assert 'Other Alliance' in players_field.value
            absent = build_zone_embed(snapshot, config, 202)
            assert 'not ranked' in absent.description
    asyncio.run(run())

def test_settings_store() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        path = tmp / 'settings.json'
        store = SettingsStore(path)
        assert store.get('report_time') is None
        try:
            store.update({'bogus': 1})
        except ValueError:
            pass
        else:
            raise AssertionError('unknown settings keys should be rejected')
        store.update({'report_time': '07:30', 'timezone': 'America/New_York'})
        assert store.get('report_time') == '07:30'
        reloaded = SettingsStore(path)
        assert reloaded.get('timezone') == 'America/New_York'
        store.update({'timezone': None})
        assert reloaded.get('timezone') is None or SettingsStore(path).get('timezone') is None


def test_report_schedules_are_scoped_and_migrate() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        path = Path(tmp_s) / 'settings.json'
        store = SettingsStore(path)
        first = store.add_schedule(guild_id=101, channel_id=201, name='Morning', report_time='07:30', timezone='UTC', mode='today')
        same_server = store.add_schedule(guild_id=101, channel_id=202, name='Lunch', report_time='12:00', timezone='UTC', mode='today')
        second = store.add_schedule(guild_id=202, channel_id=202, name='Evening', report_time='19:00', timezone='America/New_York', mode='week')
        assert [item['id'] for item in store.list_schedules(101)] == [first['id'], same_server['id']]
        assert [item['id'] for item in store.list_schedules(202)] == [second['id']]
        assert store.update_schedule(first['id'], 101, {'enabled': False})['enabled'] is False
        assert store.remove_schedule(second['id'], 101) is False
        assert store.remove_schedule(second['id'], 202) is True

        legacy_path = Path(tmp_s) / 'legacy.json'
        legacy = SettingsStore(legacy_path)
        legacy.update({'report_time': '23:45', 'timezone': 'UTC', 'report_mode': 'today', 'channel_id': 333})
        migrated = legacy.migrate_legacy(guild_id=303, fallback_channel_id=None, default_time='01:00', default_timezone='UTC', default_mode='week')
        assert migrated is not None
        assert migrated['guild_id'] == 303 and migrated['channel_id'] == 333
        assert legacy.migrate_legacy(guild_id=303, fallback_channel_id=None, default_time='01:00', default_timezone='UTC', default_mode='week') is None


def test_schedule_next_run_respects_timezone() -> None:
    before = datetime(2026, 1, 1, 11, 59, tzinfo=timezone.utc)
    schedule = {'time': '12:00', 'timezone': 'UTC'}
    assert _schedule_next_run(schedule, before) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    after = datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)
    assert _schedule_next_run(schedule, after) == datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)

    eastern = {'time': '07:00', 'timezone': 'America/New_York'}
    assert _schedule_next_run(eastern, before) == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_management_commands_are_guild_only_and_gated() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        bot = WplaceLeaderboardBot(make_config(Path(tmp_s)), Secrets())
        commands = {command.name: command for command in bot.tree.get_commands()}
        assert commands['leaderboard'].default_permissions is None
        for name in ('refresh', 'settings', 'schedule-add', 'schedule-list', 'schedule-remove', 'schedule-enable', 'schedule-test', 'channel-set', 'daily-test'):
            command = commands[name]
            assert command.guild_only is True
            assert command.default_permissions.manage_guild is True


def test_due_schedules_share_collection_by_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        async def run() -> None:
            bot = WplaceLeaderboardBot(make_config(Path(tmp_s)), Secrets())
            bot.settings.add_schedule(guild_id=101, channel_id=201, name='First', report_time='12:00', timezone='UTC', mode='today')
            bot.settings.add_schedule(guild_id=101, channel_id=202, name='Second', report_time='12:00', timezone='UTC', mode='today')
            bot.settings.add_schedule(guild_id=101, channel_id=203, name='Third', report_time='12:00', timezone='UTC', mode='week')
            collected: list[str] = []
            posted: list[int] = []

            async def collect(mode: str) -> dict:
                collected.append(mode)
                return {'mode': mode}

            async def resolve(channel_id: int):
                return channel_id

            async def post(channel_id: int, snapshot: dict) -> int:
                posted.append(channel_id)
                return 1

            bot.collect = collect
            bot.resolve_report_channel = resolve
            bot.post_daily_sequence = post
            await bot._run_due_schedules(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
            assert sorted(collected) == ['today', 'week']
            assert sorted(posted) == [201, 202, 203]

        asyncio.run(run())

def test_stores_roundtrip_and_prune() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        snapshots = SnapshotStore(tmp / 'snapshots.json')
        old = {'collected_at': (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(), 'mode': 'today', 'zones': {}}
        new = {'collected_at': datetime.now(timezone.utc).isoformat(), 'mode': 'today', 'zones': {}}
        snapshots._snapshots = [old]
        snapshots.append(new)
        assert snapshots.latest('today')['collected_at'] == new['collected_at']
        assert len(snapshots._snapshots) == 1
        reloaded = SnapshotStore(tmp / 'snapshots.json')
        assert reloaded.latest('today') is not None
        assert reloaded.latest('week') is None

def test_snapshot_store_caps_snapshot_count() -> None:
    with tempfile.TemporaryDirectory() as tmp_s:
        snapshots = SnapshotStore(Path(tmp_s) / 'snapshots.json')
        collected_at = datetime.now(timezone.utc).isoformat()
        snapshots._snapshots = [
            {'collected_at': collected_at, 'mode': 'today', 'marker': index}
            for index in range(snapshots.MAX_SNAPSHOTS + 1)
        ]
        snapshots._prune()
        assert len(snapshots._snapshots) == snapshots.MAX_SNAPSHOTS
        assert snapshots._snapshots[0]['marker'] == 1
        assert snapshots._snapshots[-1]['marker'] == snapshots.MAX_SNAPSHOTS

def test_config_validation() -> None:
    from wplace_lb.config import ConfigError
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        env = tmp / '.env'
        env.write_text('DISCORD_GUILD_IDS=101, 202 303\nDISCORD_CHANNEL_ID=404\n')
        secrets = Secrets.load(env)
        assert secrets.discord_guild_ids == (101, 202, 303)
        assert secrets.discord_guild_id == 101
        assert secrets.discord_channel_id == 404
        import json
        bad = tmp / 'bad.json'
        bad.write_text(json.dumps({'alliance_name': 'x', 'zones': [], 'daily_report_time': '25:00'}))
        try:
            Config.load(bad)
        except ConfigError:
            pass
        else:
            raise AssertionError('empty zones should fail validation')
        bad_time = tmp / 'bad_time.json'
        bad_time.write_text(json.dumps({'alliance_name': 'x', 'zones': [1], 'daily_report_time': '25:00'}))
        try:
            Config.load(bad_time)
        except ConfigError:
            pass
        else:
            raise AssertionError('bad time should fail validation')

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for test in tests:
        try:
            test()
            print(f'PASS {test.__name__}')
        except AssertionError as exc:
            failed += 1
            print(f'FAIL {test.__name__}: {exc}')
        except Exception as exc:
            failed += 1
            print(f'ERROR {test.__name__}: {type(exc).__name__}: {exc}')
    print(f'\n{len(tests) - failed}/{len(tests)} checks passed')
    return 1 if failed else 0
if __name__ == '__main__':
    sys.exit(main())
