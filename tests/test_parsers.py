"""Тесты парсеров на сохраненных страницах ЕИС.

Запуск: cd src && ../.venv/bin/python -m pytest ../tests -q
(или ../.venv/bin/python ../tests/test_parsers.py — тесты работают и без pytest)
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tender_radar.parsers import (  # noqa: E402
    parse_contract_card,
    parse_contract_search,
    parse_total_records,
)

FIXTURES = ROOT / "tests" / "fixtures"
SEARCH = FIXTURES / "search_page.html"
CARD = FIXTURES / "contract_card.html"
RAW = ROOT / "data" / "raw"   # накопленные страницы: есть не всегда


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_search_page_returns_full_page_of_contracts():
    rows = parse_contract_search(_read(SEARCH))
    assert len(rows) == 50, "на странице выдачи должно быть 50 карточек"
    for row in rows:
        assert row["reestr_number"].isdigit() and len(row["reestr_number"]) == 19
        assert row["customer_name"], "заказчик не разобран"
        assert row["published_at"] is None or isinstance(row["published_at"], date)


def test_search_page_parses_prices_and_dates():
    rows = parse_contract_search(_read(SEARCH))
    with_price = [r for r in rows if r["price"] is not None]
    assert len(with_price) >= 40, "цена должна разбираться у большинства контрактов"
    assert all(r["price"] >= 0 for r in with_price)
    dated = [r for r in rows if r["published_at"]]
    assert len(dated) >= 40, "дата размещения должна разбираться у большинства"


def test_total_records_counter():
    total, exact = parse_total_records(_read(SEARCH))
    assert total == 10000
    assert exact is False, "«более 10 000» — это не точное число"


def test_contract_card_core_fields():
    card = parse_contract_card(_read(CARD))
    assert card["reestr_number"] == "2780703321526000056"
    assert card["price"] == 5456100.0
    assert card["advance_pct"] == 30.0
    assert card["signed_at"] == date(2026, 9, 14)
    assert card["execution_until"] == date(2027, 12, 31)
    assert "ТОПЛИВНО-ЭНЕРГЕТИЧЕСКИЙ" in card["suppliers"][0]["name"]
    assert card["suppliers"][0]["inn"] == "7838129301"
    assert card["suppliers"][0]["email"] == "info@aotek.spb.ru"
    assert card["suppliers"][0]["address"].startswith("190000")


def test_missing_advance_field_means_zero():
    """ЕИС не выводит «Размер аванса», когда аванса нет, — это 0 %, а не пропуск."""
    cards = sorted(RAW.glob("card_*.html"))[:40]
    if not cards:
        return
    parsed = [parse_contract_card(_read(p)) for p in cards]
    assert all(c["advance_pct"] is not None for c in parsed), "аванс не должен быть None"
    assert any(c["advance_pct"] == 0.0 for c in parsed)


def test_every_saved_card_parses_supplier_with_inn():
    cards = sorted(RAW.glob("card_*.html"))[:60]
    if not cards:
        return
    without = [p.name for p in cards
               if not any(s["inn"] for s in parse_contract_card(_read(p))["suppliers"])]
    assert not without, f"карточки без ИНН поставщика: {without[:5]}"


def test_supplier_columns_are_found_by_header():
    """Состав колонок в таблице поставщика разный: где-то есть «Почтовый адрес»,
    где-то нет. Разбор по номеру колонки давал адрес вместо телефона."""
    cards = sorted(RAW.glob("card_*.html"))
    if not cards:
        return
    parsed = [s for p in cards for s in parse_contract_card(_read(p))["suppliers"]]
    assert parsed, "поставщики не разобраны"
    without_mail = [s["name"][:30] for s in parsed if not s["email"]]
    assert not without_mail, f"поставщики без почты: {without_mail[:3]}"
    in_phone = [s["phone"][:40] for s in parsed if any(w in s["phone"] for w in ("УЛ", "обл.", "Д."))]
    assert not in_phone, f"в телефон попал адрес: {in_phone[:3]}"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
                passed += 1
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
