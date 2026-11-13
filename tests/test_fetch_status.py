import sys, tempfile, sqlite3, unittest, fcntl
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from tender_radar.db import Database
from tender_radar.fetch_status import track_fetch, FetchAlreadyRunning
from tender_radar import cli

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
if __name__=='__main__':unittest.main()
