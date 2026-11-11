"""Настройки владельца и позиция чтения Telegram, отдельно от исследовательской базы."""
from __future__ import annotations
import json
import math
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

STATE_PATH = ROOT / 'data' / 'bot_state.db'

@dataclass
class Filters:
    region: str = ''
    min_price: float = 1_000_000

    def matches(self, lead, addresses):
        if lead.price < self.min_price:
            return False
        region = self.region.casefold()
        if not region:
            return True
        if region.isdigit():
            return lead.inn.startswith(region)
        return region in addresses.get(lead.inn, '').casefold()

    def description(self):
        region = self.region or 'все'
        return f'Регион регистрации поставщика: {region}; сумма от {self.min_price:,.0f} руб.'.replace(',', ' ')

class State:
    def __init__(self, path=STATE_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, key, default=None):
        row = self.conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.conn.execute('INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (key, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def filters(self):
        return Filters(**self.get('filters', {}))

    def close(self):
        self.conn.close()


def parse_filters(text, current):
    import shlex
    if text.strip().lower() == 'reset':
        return Filters()
    new = Filters(**asdict(current))
    for token in shlex.split(text):
        key, sep, value = token.partition('=')
        if not sep:
            raise ValueError('Используйте регион=Саратов сумма=5000000 или region=64 min=5000000.')
        if key in ('регион', 'region'):
            if len(value) > 100 or (value.isdigit() and len(value) != 2):
                raise ValueError('Регион: часть адреса или двузначный код из ИНН, например 64.')
            new.region = '' if value.lower() in ('all', 'все') else value
        elif key in ('сумма', 'min'):
            number = float(value.replace(',', '.'))
            if not math.isfinite(number) or number < 1_000_000 or number > 1e15:
                raise ValueError('Минимальная сумма должна быть от 1000000 до 1000000000000000 рублей.')
            new.min_price = number
        else:
            raise ValueError('Допустимые параметры: регион и сумма (region и min).')
    return new
