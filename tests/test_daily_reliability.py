"""Проверка сбоев ежедневного запуска без сети и рабочей базы."""
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from tender_radar import cli, digest
from tender_radar.db import Database


class DailyReliabilityTests(unittest.TestCase):
    def test_fetch_failure_still_sends_digest_and_skips_backfill(self):
        self.check_stages('fetch', ['fetch', 'digest'])

    def test_backfill_failure_still_sends_digest(self):
        self.check_stages('backfill-contacts', ['fetch', 'backfill-contacts', 'digest'])

    def test_delivery_failure_is_nonzero(self):
        self.check_stages('digest', ['fetch', 'backfill-contacts', 'digest'])

    def test_successful_daily_run(self):
        self.check_stages('', ['fetch', 'backfill-contacts', 'digest'])

    def check_stages(self, failing, expected):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'scripts').mkdir()
            (root / 'src').mkdir()
            (root / 'bin').mkdir()
            shutil.copy(ROOT / 'scripts/run_daily.sh', root / 'scripts/run_daily.sh')
            shim = root / 'bin/caffeinate'
            shim.write_text('#!/bin/zsh\nprint -r -- "$7" >> "$CALLS"\n[[ "$7" != "$FAIL_STAGE" ]]\n')
            shim.chmod(0o755)
            calls = root / 'calls'
            result = subprocess.run(['zsh', str(root / 'scripts/run_daily.sh')],
                env={**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH'],
                     'CALLS': str(calls), 'FAIL_STAGE': failing}, capture_output=True, text=True)
            self.assertEqual(calls.read_text().splitlines(), expected, result.stderr)
            self.assertEqual(result.returncode, 1 if failing else 0, result.stderr)

    def test_transient_telegram_failure_is_retried_without_logging_token(self):
        with patch.object(digest.urllib.request, 'urlopen', side_effect=[
                URLError('secret-token'), io.BytesIO(b'{"ok":true}')]) as send, \
                patch.object(digest.time, 'sleep'), self.assertLogs(digest.log, 'ERROR') as logs:
            self.assertTrue(digest.send_message('test', {'TELEGRAM_TOKEN': 'secret-token', 'TELEGRAM_CHAT_ID': '1'}))
            self.assertEqual(send.call_count, 2)
            self.assertNotIn('secret-token', '\n'.join(logs.output))

    def test_failed_delivery_does_not_mark_leads(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / 'test.db'
            db = Database(db_path)
            db.conn.execute("""INSERT INTO contracts
                (reestr_number, price, advance_pct, signed_at, execution_until)
                VALUES ('111', 20000000, 0, '2026-11-03', '2027-11-03')""")
            db.conn.execute("INSERT INTO suppliers (inn, name, email) VALUES ('7701234567', 'ООО Тест', 'a@example.org')")
            db.conn.execute("INSERT INTO contract_suppliers VALUES ('111', '7701234567')")
            db.conn.commit()
            db.conn.close()
            with patch.object(digest, 'send_message', return_value=False):
                with self.assertRaises(digest.DigestDeliveryError):
                    digest.run_digest(db_path, date(2026, 11, 4), env={})
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM digest_sent').fetchone()[0], 0)
                self.assertEqual(len(digest.select_leads(conn, date(2026, 11, 4), 7, 14)), 1)

    def test_eis_failure_returns_nonzero(self):
        for command, internal in [(cli.cmd_fetch, '_fetch'), (cli.cmd_backfill, '_backfill')]:
            with tempfile.TemporaryDirectory() as temp, patch.object(cli, 'DB_PATH', Path(temp)/'test.db'), patch.object(cli, internal, side_effect=RuntimeError('offline')):
                with self.assertRaises(SystemExit) as error:
                    command(None)
                self.assertEqual(error.exception.code, 2)

    def test_stale_data_is_labelled(self):
        message = digest.build_messages([], date(2026, 11, 4), date(2026, 11, 3))[0]
        self.assertIn('03.11.2026', message)
        self.assertIn('Сегодня база не обновлялась', message)


if __name__ == '__main__':
    unittest.main()
