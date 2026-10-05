from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo
from .collector import alliance_members
from .config import Config
MODE_LABELS = {'today': 'Today', 'week': 'Week', 'month': 'Month', 'all-time': 'All-time'}
ALLIANCE_MARKER = '✅'
PODIUM_EMOJIS = {
    1: '🥇',
    2: '🥈',
    3: '🥉',
}

def _rank_label(rank: int) -> str:
    badge = PODIUM_EMOJIS.get(rank)
    return f'{rank}{f" {badge}" if badge else ""}'

def _zone_label(zone: int, entry: dict) -> str:
    name = entry.get('region_name') if entry else ''
    return f'#{zone} {name}' if name else f'#{zone}'

def _format_collected(snapshot: dict, config: Config) -> str:
    try:
        when = datetime.fromisoformat(snapshot['collected_at']).astimezone(ZoneInfo(config.timezone))
    except (KeyError, ValueError):
        return 'unknown time'
    return when.strftime('%Y-%m-%d %H:%M %Z').strip()

def _delta(entry: dict, previous: dict | None, zone: int) -> str:
    if not previous or not entry.get('present'):
        return ''
    prev = previous.get('zones', {}).get(str(zone))
    if not prev or not prev.get('present') or prev.get('pixels') is None:
        return ''
    diff = int(entry.get('pixels') or 0) - int(prev['pixels'])
    if diff == 0:
        return ' (=)'
    return f" ({('+' if diff > 0 else '')}{diff:,})"

def _members_text(entry: dict, snapshot: dict, max_names: int=3) -> str:
    players = entry.get('players') or []
    if not players:
        return ''
    alliance = snapshot.get('alliance', {})
    members = alliance_members(players, alliance.get('id'), alliance.get('name', ''))
    if members:
        names = ' · '.join((f"{m['name']} {m['pixels']:,}" for m in members[:max_names]))
        more = len(members) - max_names
        return names + (f' (+{more} more)' if more > 0 else '')
    top = players[0]
    return f"Top painter: {top['name']} ({top['alliance_name'] or 'no alliance'}) {top['pixels']:,}"

def format_report(snapshot: dict, config: Config, previous: dict | None=None) -> str:
    mode = snapshot.get('mode', 'today')
    alliance = snapshot.get('alliance', {}).get('name', config.alliance_name)
    lines = [f'{alliance} — Wplace {MODE_LABELS.get(mode, mode)} leaderboard', '']
    total = 0
    for zone in config.zones:
        entry = snapshot.get('zones', {}).get(str(zone))
        if entry is None:
            lines.append(f'#{zone:<4} no data')
            continue
        if entry.get('error') == 'zone unresolved':
            lines.append(f'#{zone:<4} zone unresolved (no region ID)')
            continue
        if entry.get('error'):
            lines.append(f"#{zone:<4} error: {entry['error']}")
            continue
        label = _zone_label(zone, entry)
        if entry.get('present'):
            pixels = int(entry.get('pixels') or 0)
            total += pixels
            lines.append(f"{label:<20} {pixels:>9,} px  (rank {entry.get('rank')}){_delta(entry, previous, zone)}")
            members = _members_text(entry, snapshot)
            if members:
                lines.append(f'  {members}')
        else:
            lines.append(f'{label:<20} not ranked')
    lines.append('')
    lines.append(f'Total (ranked zones): {total:,} px')
    lines.append(f'Collected: {_format_collected(snapshot, config)}')
    return '\n'.join(lines)

def _is_ours(row_id: int, row_name: str, alliance: dict) -> bool:
    if alliance.get('id') is not None and row_id and (int(row_id) == int(alliance['id'])):
        return True
    return bool(row_name) and row_name.casefold() == str(alliance.get('name', '')).casefold()

def _our_alliance_label(entry: dict) -> str:
    if entry.get('present'):
        return f"Our alliance: **{int(entry.get('pixels') or 0):,} px** at rank {entry.get('rank')}"
    return 'Our alliance: **not ranked** (below the top-50 cutoff)'

def build_zone_embed(snapshot: dict, config: Config, zone: int):
    import discord
    mode = snapshot.get('mode', 'today')
    alliance = snapshot.get('alliance', {})
    entry = snapshot.get('zones', {}).get(str(zone))
    region_name = (entry or {}).get('region_name') or 'Zone'
    embed = discord.Embed(title=f'{region_name} #{zone} — {MODE_LABELS.get(mode, mode)}', color=3447003)
    if entry is None:
        embed.description = 'No data collected for this zone yet.'
        return embed
    if entry.get('error'):
        embed.description = f"Collection failed: {entry['error']}"
        return embed
    embed.description = _our_alliance_label(entry)
    lines: list[str] = []
    ours_on_board = False
    for row in entry.get('alliances') or []:
        ours = _is_ours(row['id'], row['name'], alliance)
        marker = f' {ALLIANCE_MARKER}' if ours else ''
        if ours:
            ours_on_board = True
            lines.append(f"**{_rank_label(row['rank'])}. {row['name']} — {row['pixels']:,}**{marker}")
        else:
            lines.append(f"{_rank_label(row['rank'])}. {row['name']} — {row['pixels']:,}")
    if entry.get('present') and (not ours_on_board):
        our_name = alliance.get('name') or config.alliance_name
        lines.append(f"**…{_rank_label(entry.get('rank'))}. {our_name} — {int(entry.get('pixels') or 0):,}** {ALLIANCE_MARKER}")
    embed.add_field(name='Alliances', value='\n'.join(lines) or 'empty board', inline=False)
    plines: list[str] = []
    for p in entry.get('players') or []:
        ours = _is_ours(p['alliance_id'], p['alliance_name'], alliance)
        name = f"{p['name']} #{p['player_id']}" if p['player_id'] else p['name']
        if ours:
            plines.append(f"**{_rank_label(p['rank'])}. {name} — {p['pixels']:,}** {ALLIANCE_MARKER}")
        else:
            tag = f" · {p['alliance_name']}" if p['alliance_name'] else ''
            plines.append(f"{_rank_label(p['rank'])}. {name} — {p['pixels']:,}{tag}")
    embed.add_field(name='Top painters', value='\n'.join(plines) if plines else 'No painters on the board.', inline=False)
    embed.add_field(name='Legend', value=f'{ALLIANCE_MARKER} = configured alliance', inline=False)
    embed.set_footer(text=f'Collected {_format_collected(snapshot, config)}')
    return embed

def build_embed(snapshot: dict, config: Config, zone_filter: int | None=None, previous: dict | None=None):
    import discord
    mode = snapshot.get('mode', 'today')
    alliance = snapshot.get('alliance', {}).get('name', config.alliance_name)
    title_scope = f'zone #{zone_filter}' if zone_filter is not None else 'all zones'
    embed = discord.Embed(title=f'{alliance} — Wplace {MODE_LABELS.get(mode, mode)}', description=f'Pixels painted by our alliance in {title_scope}.', color=3447003)
    zones = [zone_filter] if zone_filter is not None else config.zones
    total = 0
    ranked = 0
    unresolved = 0
    for zone in zones:
        entry = snapshot.get('zones', {}).get(str(zone))
        if entry is None:
            embed.add_field(name=f'#{zone}', value='no data', inline=True)
            continue
        if entry.get('error') == 'zone unresolved':
            unresolved += 1
            embed.add_field(name=f'#{zone}', value='zone unresolved', inline=True)
            continue
        if entry.get('error'):
            embed.add_field(name=f'#{zone}', value=f"error: {entry['error']}"[:200], inline=True)
            continue
        if entry.get('present'):
            pixels = int(entry.get('pixels') or 0)
            total += pixels
            ranked += 1
            members = _members_text(entry, snapshot)
            value = f"**{pixels:,}** px — rank {entry.get('rank')}{_delta(entry, previous, zone)}"
            if members:
                value += f'\n-# {members}'
            embed.add_field(name=_zone_label(zone, entry), value=value, inline=True)
        else:
            embed.add_field(name=_zone_label(zone, entry), value='not ranked', inline=True)
    if ranked:
        embed.description += f'\n\n**Total (ranked zones): {total:,} px across {ranked} zone(s).**'
    if unresolved:
        embed.description += f'\n{unresolved} zone(s) could not be mapped to a region ID yet — run `resolve`.'
    embed.set_footer(text=f'Collected {_format_collected(snapshot, config)} · absent means below the top-50 cutoff')
    return embed
