"""Повторный разбор сохраненного HTML: лимит, целостность и отказ источника."""
import copy
from datetime import date
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tender_radar.db import Database
from tender_radar.client import BlockedError
from tender_radar.maintenance import maintain_cards, validate_card
from tender_radar.parsers import parse_contract_card

HTML = (Path(__file__).parent / 'fixtures/contract_card.html').read_text()
TODAY = date(2026, 11, 13)


def html_for(number):
    soup = BeautifulSoup(HTML, 'lxml')
    label = next(x for x in soup.select('span.section__title')
                 if x.get_text(strip=True).startswith('Реестровый номер контракта'))
    info = label.find_parent('section').select_one('span.section__info')
    info.clear()
    info.append(number)
    return str(soup)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.db')
        self.card = parse_contract_card(HTML)
        self.number = self.card['reestr_number']
        self.db.conn.execute('INSERT INTO contracts (reestr_number) VALUES (?)', (self.number,))
        self.db.save_card(self.card)
        self.db.conn.execute("UPDATE contracts SET price=2000000,advance_pct=0,signed_at='2026-11-01',execution_until='2027-01-01',card_fetched_at='2026-11-01 00:00:00'")
        self.db.commit()

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_refresh_replaces_links_without_duplicates_and_keeps_first_seen(self):
        before = self.db.conn.execute('SELECT first_seen_at FROM contracts').fetchone()[0]
        old = self.card['suppliers'][0]['inn']
        card = copy.deepcopy(self.card)
        card['suppliers'][0].update(inn='6400000001', email='new@example.org')
        self.db.save_card(card)
        self.db.save_card(card)
        self.db.commit()
        links = self.db.conn.execute('SELECT inn FROM contract_suppliers').fetchall()
        self.assertEqual([r[0] for r in links], ['6400000001'])
        self.assertEqual(self.db.conn.execute('SELECT first_seen_at FROM contracts').fetchone()[0], before)
        self.assertEqual(self.db.conn.execute("SELECT email FROM suppliers WHERE inn='6400000001'").fetchone()[0], 'new@example.org')
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM contracts').fetchone()[0], 1)
        # Старый поставщик может относиться к другим контрактам, его не удаляем.
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM suppliers WHERE inn=?', (old,)).fetchone()[0], 1)

    def test_failed_save_rolls_back_card_and_links(self):
        card = copy.deepcopy(self.card)
        card['price'] = 9999999
        card['suppliers'] = [dict(inn='6400000001')]
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.save_card(card)
        self.assertEqual(self.db.conn.execute('SELECT price FROM contracts').fetchone()[0], 2000000)
        self.assertEqual(self.db.conn.execute('SELECT inn FROM contract_suppliers').fetchone()[0], self.card['suppliers'][0]['inn'])

    def test_existing_supplier_fields_are_refreshed(self):
        card = copy.deepcopy(self.card)
        card['suppliers'][0].update(kpp='640101001', country='Россия', email='changed@example.org')
        self.db.save_card(card)
        row = self.db.conn.execute('SELECT kpp,country,email FROM suppliers WHERE inn=?',
                                   (card['suppliers'][0]['inn'],)).fetchone()
        self.assertEqual(tuple(row), ('640101001', 'Россия', 'changed@example.org'))

    def test_missing_contract_is_not_saved_as_orphan(self):
        card = copy.deepcopy(self.card)
        card['reestr_number'] = '9999999999999999999'
        with self.assertRaises(ValueError):
            self.db.save_card(card)
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM contract_suppliers').fetchone()[0], 1)

    def test_stale_policy_and_changed_summary(self):
        self.assertEqual(self.db.stale_cards(5, 7, TODAY), [self.number])
        self.db.conn.execute("UPDATE contracts SET card_fetched_at='2026-11-12 00:00:00'")
        self.assertEqual(self.db.stale_cards(5, 7, TODAY), [])
        self.db.conn.execute("UPDATE contracts SET updated_at='2026-11-13'")
        self.assertEqual(self.db.stale_cards(5, 7, TODAY), [self.number])
        for column, value in [('status', 'Исполнение завершено'), ('signed_at', '2026-10-01'), ('execution_until', '2026-11-12')]:
            self.db.conn.execute(f'UPDATE contracts SET {column}=?', (value,))
            self.assertEqual(self.db.stale_cards(5, 7, TODAY), [])
            self.db.conn.execute("UPDATE contracts SET status='Исполнение',signed_at='2026-11-01',execution_until='2027-01-01'")

    def test_refresh_uses_saved_html_and_updates_card(self):
        client = Mock()
        client.get.return_value = HTML
        result = maintain_cards(self.db, client, limit=1, refresh_limit=1, today=TODAY)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(self.db.conn.execute('SELECT price FROM contracts').fetchone()[0], self.card['price'])

    def test_total_budget_includes_refresh_and_contact_backfill(self):
        # Подготовлены 25 поставщиков, ответы получаются из сохраненного fixture.
        for i in range(25):
            number = str(1000000000000000000 + i)
            inn = str(6400000000 + i)
            self.db.conn.execute("INSERT INTO contracts (reestr_number,price) VALUES (?,5000000)", (number,))
            self.db.conn.execute("INSERT INTO suppliers(inn,name,email) VALUES (?,'ООО Тест','')", (inn,))
            self.db.conn.execute('INSERT INTO contract_suppliers VALUES (?,?)', (number, inn))
        self.db.commit()
        client = Mock()
        client.get.side_effect = lambda path, params, **kw: html_for(params['reestrNumber'])
        result = maintain_cards(self.db, client, limit=20, refresh_limit=1, today=TODAY)
        self.assertEqual(result['requests'], 20)
        self.assertEqual(result['refreshed'], 1)
        self.assertEqual(client.get.call_count, 20)

    def test_blocking_stops_before_backfill(self):
        client = Mock()
        client.get.side_effect = BlockedError('blocked')
        with self.assertRaises(BlockedError):
            maintain_cards(self.db, client, today=TODAY)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(self.db.conn.execute('SELECT price FROM contracts').fetchone()[0], 2000000)

    def test_incomplete_or_wrong_card_preserves_previous_data(self):
        for html in ['<html>error</html>', html_for('9999999999999999999')]:
            client = Mock()
            client.get.return_value = html
            with self.assertRaises(RuntimeError):
                maintain_cards(self.db, client, today=TODAY)
            self.assertEqual(self.db.conn.execute('SELECT price FROM contracts').fetchone()[0], 2000000)
            self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM contract_suppliers').fetchone()[0], 1)

    def test_bad_budget_rejected_without_network(self):
        client = Mock()
        for limit, refresh, days in [(21, 5, 7), (20, 21, 7), (20, -1, 7), (20, 5, 0)]:
            with self.assertRaises(ValueError):
                maintain_cards(self.db, client, limit, refresh, days, TODAY)
        client.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
