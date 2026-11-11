"""Тесты дайджеста. Сеть не используется. Запуск: .venv/bin/python tests/test_digest.py"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tender_radar.db import Database  # noqa: E402
from tender_radar.digest import (  # noqa: E402
    TELEGRAM_LIMIT, build_messages, format_lead, select_leads, short_name, _ensure_table,
)
from tender_radar.scoring import Lead  # noqa: E402

TODAY = date(2026, 10, 27)


def _lead(name='ООО "ТЕСТ"', reestr="1234567890123456789", phone="8-800-000-00-00", email="a@b.ru"):
    signed = TODAY - timedelta(days=2)
    return Lead(inn="7700000000", name=name, reestr_number=reestr, price=12.34e6,
                signed_at=signed, execution_until=signed + timedelta(days=300),
                phone=phone, email=email, contracts_in_sample=1, score=77.7)


def test_short_name_removes_form_and_duplicate():
    assert short_name('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "МЕДФАРМТРЕЙД" (ООО "МЕДФАРМТРЕЙД")') == 'ООО "МЕДФАРМТРЕЙД"'
    assert short_name("КУЗНЕЦОВ СЕРГЕЙ АЛЕКСЕЕВИЧ Индивидуальный предприниматель") == "ИП Кузнецов Сергей Алексеевич"
    assert len(short_name("А" * 200)) <= 60


def test_external_text_is_escaped():
    """Название и контакты приходят с внешнего сайта: разметка в них не должна работать."""
    text = format_lead(1, _lead(name='ООО <b>"ЗЛО"</b> & <a href="x">', email="<i>x</i>@mail.ru"), TODAY)
    assert "<b>ООО" not in text.split("\n")[0].replace("<b>1. ", "", 1)
    assert "&lt;b&gt;" in text and "&amp;" in text and "&lt;i&gt;" in text
    assert '<a href="x">' not in text


def test_link_only_for_numeric_reestr_number():
    assert "карточка контракта" in format_lead(1, _lead(reestr="1234567890123456789"), TODAY)
    assert "карточка контракта" not in format_lead(1, _lead(reestr='1"><script>'), TODAY)


def test_message_is_split_under_telegram_limit():
    leads = [_lead(name=f'ООО "КОМПАНИЯ {i} ' + "Я" * 40 + '"') for i in range(40)]
    messages = build_messages(leads, TODAY)
    assert len(messages) > 1
    assert all(len(m) <= TELEGRAM_LIMIT + 200 for m in messages)


def test_empty_digest_says_so():
    assert "нет" in build_messages([], TODAY)[0]


def test_sent_leads_are_not_repeated():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "t.db")
        signed = TODAY - timedelta(days=2)
        db.conn.execute(
            """INSERT INTO contracts (reestr_number, price, advance_pct, signed_at, execution_until)
               VALUES ('1111111111111111111', 20000000, 0, ?, ?)""",
            (signed.isoformat(), (signed + timedelta(days=300)).isoformat()))
        db.conn.execute("INSERT INTO suppliers (inn, name, email) VALUES ('7701234567', 'ООО \"АЛЬФА\"', 'x@y.ru')")
        db.conn.execute("INSERT INTO contract_suppliers VALUES ('1111111111111111111', '7701234567')")
        db.conn.commit()
        first = select_leads(db.conn, TODAY, top=5, fresh_days=14)
        assert [l.inn for l in first] == ["7701234567"]
        _ensure_table(db.conn)
        db.conn.execute("INSERT INTO digest_sent (inn, reestr_number) VALUES ('7701234567', '1111111111111111111')")
        db.conn.commit()
        assert select_leads(db.conn, TODAY, top=5, fresh_days=14) == []


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
