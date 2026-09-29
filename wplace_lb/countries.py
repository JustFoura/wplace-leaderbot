from __future__ import annotations
import json
from importlib import resources
_CACHE: dict[str, int] | None = None

def _load() -> dict[str, int]:
    global _CACHE
    if _CACHE is None:
        raw = json.loads(resources.files(__package__).joinpath('countries.json').read_text(encoding='utf-8'))
        _CACHE = {entry['code'].upper(): int(entry['id']) for entry in raw}
    return _CACHE

def country_id_for_code(code: str) -> int | None:
    return _load().get(code.upper())

def country_name_for_id(country_id: int) -> str | None:
    for code, cid in _load().items():
        if cid == country_id:
            return code
    return None
