"""Восстановление опроса и сохраненной очереди на уровне HTTP-транспорта."""
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tender_radar import bot, digest
from tender_radar.bot_state import State
from tender_radar.db import Database


class RecoveryTests(unittest.TestCase):
    def test_poll_recovers_and_resets_backoff(self):
        with patch.object(bot, 'poll_once', side_effect=[
                bot.TelegramError('timeout'), bot.TelegramError('connection'), None,
                bot.TelegramError('timeout'), KeyboardInterrupt]), \
                patch.object(bot.time, 'sleep') as sleep, \
                self.assertLogs(bot.log, 'INFO') as logs:
            with self.assertRaises(KeyboardInterrupt):
                bot.poll_forever(None, {}, None, None)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 2])
        self.assertIn('восстановлен после 2 сбоев', '\n'.join(logs.output))

    def test_transport_error_preserves_offset_and_hides_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp) / 'state.db')
            self.addCleanup(state.close)
            state.put('offset', 42)
            api = bot.Telegram({'TELEGRAM_TOKEN': 'secret-token'})
            with patch.object(bot.urllib.request, 'urlopen', side_effect=[
                    urllib.error.URLError(TimeoutError('secret-token')),
                    io.BytesIO(b'{"ok":true,"result":[]}')]):
                with self.assertRaises(bot.TelegramError) as error:
                    bot.poll_once(api, {}, None, state)
                self.assertEqual(str(error.exception), 'timeout')
                self.assertEqual(state.get('offset'), 42)
                bot.poll_once(api, {}, None, state)
                self.assertEqual(state.get('offset'), 42)

    def test_queue_resumes_after_transport_failure_and_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.db'
            db = Database(path)
            db.conn.close()
            env = {'TELEGRAM_TOKEN': 'secret-token', 'TELEGRAM_CHAT_ID': '1'}
            day = date(2026, 11, 13)
            # Первая часть подтверждена, три попытки второй завершаются таймаутом.
            with patch.object(digest, 'build_messages', return_value=['first', 'second']), \
                    patch.object(digest.urllib.request, 'urlopen', side_effect=[
                        io.BytesIO(b'{"ok":true}'),
                        *[urllib.error.URLError(TimeoutError('secret-token')) for _ in range(3)]]), \
                    patch.object(digest.time, 'sleep'), self.assertLogs(digest.log, 'ERROR') as logs:
                with self.assertRaises(digest.DigestDeliveryError):
                    digest.run_digest(path, day, env=env)
            self.assertNotIn('secret-token', '\n'.join(logs.output))
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT next_message,completed_at FROM digest_outbox').fetchone(), (1, None))
            # Повторный вызов открывает базу заново, без состояния прошлого процесса.
            with patch.object(digest.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":true}')) as send:
                digest.run_digest(path, day, env=env, retry_pending=True)
                digest.run_digest(path, day, env=env, retry_pending=True)
            self.assertEqual(send.call_count, 1)
            self.assertIn(b'text=second', send.call_args.kwargs['data'])
            with sqlite3.connect(path) as conn:
                position, completed = conn.execute('SELECT next_message,completed_at FROM digest_outbox').fetchone()
            self.assertEqual(position, 2)
            self.assertIsNotNone(completed)


if __name__ == '__main__':
    unittest.main()
