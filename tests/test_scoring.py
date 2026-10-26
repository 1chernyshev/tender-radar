"""Тесты правил приоритизации. Запуск: .venv/bin/python tests/test_scoring.py"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tender_radar.scoring import (  # noqa: E402
    WEIGHTS, Lead, duration_part, freshness_part, is_excluded_org, score_lead, size_part,
)

TODAY = date(2026, 10, 21)


def _lead(price=10e6, days=180, age=2, contracts=1):
    signed = TODAY - timedelta(days=age)
    return Lead(inn="7700000000", name='ООО "ТЕСТ"', reestr_number="1", price=price,
                signed_at=signed, execution_until=signed + timedelta(days=days),
                phone="8-800-000-00-00", email="", contracts_in_sample=contracts)


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100


def test_size_is_log_scaled_and_capped():
    assert size_part(1e6) == 0.0
    assert size_part(50e6) == 1.0
    assert size_part(500e6) == 1.0
    # 5 млн дальше от 1 млн, чем 45 от 40: логарифмическая шкала
    assert size_part(5e6) - size_part(1e6) > size_part(45e6) - size_part(40e6)


def test_duration_capped_at_one_year():
    d = TODAY
    assert duration_part(d, d + timedelta(days=365)) == 1.0
    assert duration_part(d, d + timedelta(days=800)) == 1.0
    assert abs(duration_part(d, d + timedelta(days=182)) - 0.5) < 0.01


def test_freshness_decays_to_zero():
    assert freshness_part(TODAY, TODAY) == 1.0
    assert freshness_part(TODAY - timedelta(days=3), TODAY) == 1.0
    assert freshness_part(TODAY - timedelta(days=30), TODAY) == 0.0
    assert 0 < freshness_part(TODAY - timedelta(days=15), TODAY) < 1


def test_score_bounds():
    best = score_lead(_lead(price=100e6, days=400, age=0, contracts=3), TODAY)
    worst = score_lead(_lead(price=1e6, days=0, age=60, contracts=1), TODAY)
    assert best.score == 100.0
    assert worst.score == 0.0


def test_bigger_fresher_longer_wins():
    a = score_lead(_lead(price=30e6, days=300, age=1), TODAY)
    b = score_lead(_lead(price=2e6, days=60, age=20), TODAY)
    assert a.score > b.score


def test_excluded_organisations():
    assert is_excluded_org('ГОСУДАРСТВЕННОЕ ПРЕДПРИЯТИЕ КАЛУЖСКОЙ ОБЛАСТИ "КАЛУГАФАРМАЦИЯ"')
    assert is_excluded_org('ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОСТЕЛЕКОМ"')
    assert is_excluded_org('АКЦИОНЕРНОЕ ОБЩЕСТВО "АЛЬФА-БАНК"')
    assert is_excluded_org('АКЦИОНЕРНОЕ ОБЩЕСТВО "ГАЗПРОМБАНК"')
    assert not is_excluded_org('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БАНКЕТНЫЙ ЗАЛ"')
    assert not is_excluded_org('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "МЕДФАРМТРЕЙД"')


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"ok   {name}"); passed += 1
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
