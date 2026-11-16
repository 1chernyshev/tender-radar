import sys, tempfile, sqlite3, unittest, fcntl
import argparse
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from tender_radar.db import Database
from tender_radar.fetch_status import track_fetch, FetchAlreadyRunning
from tender_radar import cli
from tender_radar.client import BlockedError
from tender_radar.parsers import parse_contract_card, parse_contract_search

class FetchStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.db'
        self.db = Database(self.path)
    def tearDown(self):
        self.db.conn.close(); self.temp.cleanup()
    def rows(self):
        return [tuple(r) for r in self.db.conn.execute('SELECT status,new_records,reason FROM fetch_runs ORDER BY id')]
    def add(self):
        self.db.conn.execute("INSERT INTO contracts(reestr_number) VALUES ('123')");self.db.commit()
    def test_success_even_without_new_records(self):
        with track_fetch(self.path): pass
        self.assertEqual(self.rows(), [('success',0,'')])
    def test_unexpected_error_after_commit_is_partial(self):
        with self.assertRaises(ValueError):
            with track_fetch(self.path):self.add();raise ValueError('bad parser')
        self.assertEqual(self.rows(), [('partial',1,'ValueError')])
    def test_unexpected_error_before_progress_is_failed(self):
        with self.assertRaises(ValueError):
            with track_fetch(self.path):raise ValueError('secret')
        self.assertEqual(self.rows(), [('failed',0,'ValueError')])
    def test_keyboard_interrupt_is_recorded(self):
        with self.assertRaises(KeyboardInterrupt):
            with track_fetch(self.path):self.add();raise KeyboardInterrupt()
        self.assertEqual(self.rows()[0][0], 'partial')
    def test_stale_running_detected_on_next_start(self):
        with track_fetch(self.path):pass
        self.db.conn.execute("UPDATE fetch_runs SET status='running', finished_at=NULL, new_records=NULL");self.db.commit()
        with track_fetch(self.path):pass
        self.assertEqual(self.rows()[0], ('interrupted',None,'detected_on_next_start'))
        self.assertEqual(self.rows()[1][0], 'success')
    def test_other_process_lock_does_not_relabel_active_run(self):
        with track_fetch(self.path):
            with self.assertRaises(FetchAlreadyRunning):
                with track_fetch(self.path):pass
            self.assertEqual(self.rows()[0][0], 'running')
        self.assertEqual(len(self.rows()),1)
    def test_old_schema_migrated(self):
        self.db.conn.execute('CREATE TABLE fetch_runs (id INTEGER PRIMARY KEY, started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, status TEXT,new_records INTEGER,new_cards INTEGER,reason TEXT)')
        self.db.conn.execute("INSERT INTO fetch_runs(status) VALUES ('running')");self.db.commit()
        with track_fetch(self.path):pass
        self.assertEqual(self.rows()[0][0], 'interrupted')
    def test_cli_propagates_unexpected_error_but_records_partial(self):
        def fail(args):self.add();raise ValueError('bad parser')
        with patch.object(cli,'DB_PATH',self.path),patch.object(cli,'_fetch',side_effect=fail):
            with self.assertRaises(ValueError):cli.cmd_fetch(None)
        self.assertEqual(self.rows()[0][0],'partial')

    def check_blocked_fetch(self, completed_cards):
        fixtures = Path(__file__).parent / 'fixtures'
        card_html = (fixtures / 'contract_card.html').read_text()
        first_number = parse_contract_card(card_html)['reestr_number']
        row = parse_contract_search((fixtures / 'search_page.html').read_text())[0]
        first = dict(row, reestr_number=first_number)
        second = dict(row, reestr_number='9999999999999999999')
        args = argparse.Namespace(delay=0, keep_raw=False, date_from='14.11.2026',
                                  date_to='15.11.2026', days=1, pages=1, cards=True)
        replies = ['search'] + [card_html] * completed_cards + [BlockedError('blocked')]
        with patch.object(cli, 'DB_PATH', self.path), patch.object(cli, 'EisClient') as client, \
                patch.object(cli, 'parse_contract_search', return_value=[first, second]):
            client.return_value.get.side_effect = replies
            with self.assertRaises(SystemExit) as error:
                cli.cmd_fetch(args)
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.rows(), [('partial' if completed_cards else 'failed', completed_cards, 'BlockedError')])
        # Незавершенная карточка не оставляет ни строки, ни блокировки записи.
        self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM contracts').fetchone()[0], completed_cards)
        self.db.conn.execute("INSERT INTO contracts(reestr_number) VALUES ('after-error')")
        self.db.commit()

    def test_blocked_before_first_card_records_failure_without_locked_database(self):
        self.check_blocked_fetch(0)

    def test_blocked_after_committed_card_records_partial_without_locked_database(self):
        self.check_blocked_fetch(1)
if __name__=='__main__':unittest.main()
