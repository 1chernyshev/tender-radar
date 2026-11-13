"""Проверки отбора на границах и согласованности CSV с Telegram."""
import argparse
import csv
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tender_radar.db import Database
from tender_radar.scoring import rank, is_excluded_org
from tender_radar import cli

TODAY = date(2026, 11, 13)


class ScoringEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.path = self.root / 'data/test.db'
        self.db = Database(self.path)
        self.db.conn.execute("INSERT INTO suppliers(inn,name,email) VALUES ('6400000001','ООО Тест','a@example.org')")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def add(self, number='1', price=5000000, advance=0, signed='2026-11-12', until='2027-01-01', status='Исполнение'):
        self.db.conn.execute('INSERT INTO contracts(reestr_number,price,advance_pct,signed_at,execution_until,status) VALUES (?,?,?,?,?,?)',
                             (number, price, advance, signed, until, status))
        self.db.conn.execute('INSERT INTO contract_suppliers VALUES (?,\'6400000001\')', (number,))
        self.db.commit()

    def test_minimum_price_inclusive_unknown_or_positive_advance_excluded(self):
        self.add('low', price=999999)
        self.add('unknown', advance=None)
        self.add('positive', advance=1)
        self.add('minimum', price=1000000)
        self.assertEqual([l.reestr_number for l in rank(self.db.conn, TODAY)[0]], ['minimum'])

    def test_frequency_seven_kept_eight_excluded_even_if_other_contracts_ineligible(self):
        self.add()
        for i in range(2, 8):
            self.add(str(i), advance=30)
        self.assertEqual(len(rank(self.db.conn, TODAY)[0]), 1)
        self.add('8', advance=30)
        self.assertEqual(rank(self.db.conn, TODAY)[0], [])

    def test_end_today_kept_yesterday_excluded(self):
        self.add('yesterday', until='2026-11-12')
        self.add('today', until='2026-11-13')
        self.assertEqual([l.reestr_number for l in rank(self.db.conn, TODAY)[0]], ['today'])

    def test_terminal_status_excluded_despite_future_end_date(self):
        self.add('finished', status='Исполнение завершено')
        self.add('terminated', status='Исполнение прекращено')
        self.assertEqual(rank(self.db.conn, TODAY)[0], [])

    def test_missing_contact_only_excluded_when_requested(self):
        self.add()
        self.db.conn.execute("UPDATE suppliers SET email='',phone='нет'")
        self.assertEqual(len(rank(self.db.conn, TODAY)[0]), 1)
        self.assertEqual(rank(self.db.conn, TODAY, candidate_filter=lambda l:l.has_contact)[0], [])

    def test_csv_filters_before_choosing_largest_contract(self):
        self.add('old-large', price=40000000, signed='2026-09-01')
        self.add('fresh-small', price=2000000)
        args = argparse.Namespace(date='13.11.2026', fresh_days=14, with_contact=True, top=20)
        with patch.object(cli, 'DB_PATH', self.path), patch.object(cli, 'ROOT', self.root):
            cli.cmd_leads(args)
        with (self.root / 'data/leads_2026-11-13.csv').open(encoding='utf-8-sig') as f:
            rows = list(csv.reader(f, delimiter=';'))
        self.assertEqual(len(rows), 2)
        self.assertIn('fresh-small', rows[1])

    def test_legal_name_and_surname_are_not_confused(self):
        self.assertTrue(is_excluded_org('ПАО «Тест»'))
        self.assertTrue(is_excluded_org('АО «Страховая компания»'))
        self.assertTrue(is_excluded_org('АО «Альфастрахование»'))
        self.assertFalse(is_excluded_org('ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ СТРАХОВ ИВАН'))
        self.assertFalse(is_excluded_org('ООО «Банкетный зал»'))


if __name__ == '__main__':
    unittest.main()
