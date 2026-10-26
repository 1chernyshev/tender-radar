"""Правила приоритизации поставщиков: кому банку предлагать финансирование первым.

Логика в два шага.

1. Фильтр: кто вообще может быть клиентом. Контракт без аванса, от 1 млн рублей,
   еще исполняется. Из поставщиков исключаются государственные и муниципальные
   организации, публичные акционерные общества и крупные системные игроки: им
   кредит под отдельный контракт не нужен.

2. Балл от 0 до 100: насколько клиент перспективен. Складывается из четырех
   признаков, каждый с объяснимым смыслом. Веса подобраны вручную и проверены
   на устойчивость (см. docs/scoring.md).

Тип предмета контракта (товары, работы, услуги) сознательно не используется:
проверка на данных не подтвердила, что он связан с глубиной кассового разрыва.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

# Веса признаков. В сумме 100.
WEIGHTS = {
    "size": 45,       # сумма контракта
    "duration": 25,   # срок исполнения
    "freshness": 20,  # как давно подписан контракт
    "activity": 10,   # поставщик регулярно участвует в закупках
}

MIN_PRICE = 1_000_000          # ниже кредит под контракт банку не окупается
SIZE_CAP = 50_000_000          # выше 50 млн балл за сумму не растет
DURATION_CAP_DAYS = 365        # выше года балл за срок не растет
FRESH_FULL_DAYS = 3            # подписан не позже 3 дней назад: полный балл
FRESH_ZERO_DAYS = 30           # старше 30 дней: ноль, клиент уже нашел деньги
# Наша выборка около 1 % потока. Поставщик, попавший в нее 8 и более раз
# за период наблюдений, в полном потоке заключает сотни контрактов: это крупный
# игрок, а не клиент на кредит под контракт.
BIG_PLAYER_MIN_CONTRACTS = 8
ACTIVE_MIN_CONTRACTS = 2

EXCLUDED_ORG_MARKERS = (
    "ГОСУДАРСТВЕННОЕ", "МУНИЦИПАЛЬНОЕ", "ФЕДЕРАЛЬНОЕ", "УНИТАРНОЕ",
    "КАЗЕННОЕ", "БЮДЖЕТНОЕ", "АВТОНОМНОЕ УЧРЕЖДЕНИЕ", "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ",
)


@dataclass
class Lead:
    inn: str
    name: str
    reestr_number: str
    price: float
    signed_at: date
    execution_until: date
    phone: str
    email: str
    contracts_in_sample: int
    score: float = 0.0
    parts: dict = field(default_factory=dict)

    @property
    def has_contact(self) -> bool:
        return bool(self.email or (self.phone and any(ch.isdigit() for ch in self.phone)))


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def size_part(price: float) -> float:
    """Логарифмическая шкала: 1 млн дает 0, 50 млн и больше дают 1.

    Логарифм, а не линейная шкала, потому что разница между 1 и 5 млн для банка
    важнее разницы между 40 и 45 млн.
    """
    lo, hi = math.log10(MIN_PRICE), math.log10(SIZE_CAP)
    return _clamp((math.log10(price) - lo) / (hi - lo))


def duration_part(signed: date, until: date) -> float:
    """Чем дольше исполняется контракт, тем дольше поставщику нужны деньги."""
    return _clamp((until - signed).days / DURATION_CAP_DAYS)


def freshness_part(signed: date, today: date) -> float:
    """Ценность лида падает со временем: поставщик ищет деньги сразу после подписания."""
    age = (today - signed).days
    if age <= FRESH_FULL_DAYS:
        return 1.0
    return _clamp(1 - (age - FRESH_FULL_DAYS) / (FRESH_ZERO_DAYS - FRESH_FULL_DAYS))


def activity_part(contracts: int) -> float:
    """Повторные контракты говорят о действующем участнике закупок."""
    return 1.0 if contracts >= ACTIVE_MIN_CONTRACTS else 0.0


# Финансовые организации тоже бывают поставщиками (банковские и страховые услуги
# госзаказчику), но клиентами на кредит под контракт не являются.
# «БАНК» в конце слова ловит и «АЛЬФА-БАНК», и «ГАЗПРОМБАНК», но не «БАНКЕТ».
FINANCIAL_ORG = re.compile(r"БАНК\b|СТРАХОВ|ЛИЗИНГ")


def is_excluded_org(name: str) -> bool:
    upper = (name or "").upper()
    if any(marker in upper for marker in EXCLUDED_ORG_MARKERS):
        return True
    return bool(FINANCIAL_ORG.search(upper))


def score_lead(lead: Lead, today: date, weights: dict | None = None) -> Lead:
    w = weights or WEIGHTS
    lead.parts = {
        "size": w["size"] * size_part(lead.price),
        "duration": w["duration"] * duration_part(lead.signed_at, lead.execution_until),
        "freshness": w["freshness"] * freshness_part(lead.signed_at, today),
        "activity": w["activity"] * activity_part(lead.contracts_in_sample),
    }
    lead.score = round(sum(lead.parts.values()), 1)
    return lead


def load_candidates(conn: sqlite3.Connection, today: date) -> tuple[list[Lead], Counter]:
    """Кандидаты после фильтра. Второе значение — сколько отсеяно и по какой причине."""
    conn.row_factory = sqlite3.Row
    counts = Counter(
        r["inn"] for r in conn.execute("SELECT inn FROM contract_suppliers")
    )
    rows = conn.execute(
        """SELECT c.reestr_number, c.price, c.signed_at, c.execution_until,
                  s.inn, s.name, s.phone, s.email
             FROM contracts c
             JOIN contract_suppliers cs ON cs.reestr_number = c.reestr_number
             JOIN suppliers s ON s.inn = cs.inn
            WHERE c.advance_pct = 0
              AND c.price >= ?
              AND c.signed_at IS NOT NULL
              AND c.execution_until IS NOT NULL""",
        (MIN_PRICE,),
    ).fetchall()

    dropped = Counter()
    best: dict[str, Lead] = {}
    for r in rows:
        until = date.fromisoformat(r["execution_until"])
        if until < today:
            dropped["контракт уже исполнен"] += 1
            continue
        if is_excluded_org(r["name"]):
            dropped["гос./муниц. организация, ПАО, банк или страховая"] += 1
            continue
        n = counts[r["inn"]]
        if n >= BIG_PLAYER_MIN_CONTRACTS:
            dropped["крупный системный игрок"] += 1
            continue
        lead = Lead(
            inn=r["inn"], name=r["name"], reestr_number=r["reestr_number"],
            price=r["price"], signed_at=date.fromisoformat(r["signed_at"]),
            execution_until=until, phone=r["phone"] or "", email=r["email"] or "",
            contracts_in_sample=n,
        )
        # У поставщика может быть несколько подходящих контрактов: берем самый крупный.
        if lead.inn not in best or lead.price > best[lead.inn].price:
            best[lead.inn] = lead
    return list(best.values()), dropped


def rank(conn: sqlite3.Connection, today: date, weights: dict | None = None) -> tuple[list[Lead], Counter]:
    leads, dropped = load_candidates(conn, today)
    for lead in leads:
        score_lead(lead, today, weights)
    leads.sort(key=lambda x: x.score, reverse=True)
    return leads, dropped
