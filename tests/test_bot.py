"""Команды, права, фильтры и состояние — без Telegram и ЕИС."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest.mock import patch
from tender_radar.bot import respond, process_update, ranked, poll_once, TelegramError
from tender_radar.bot_state import State, Filters, parse_filters
from tender_radar.db import Database
from tender_radar.digest import select_leads

TODAY = date(2026, 11, 4)
class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name)/'t.db'
        self.state_path = Path(self.tmp.name)/'s.db'
        self.state = State(self.state_path)
        self.db = Database(self.db_path)
        for number, inn, amount, signed, seen, address in [
            ('111', '6400000001', 5e6, '2026-11-03', '2026-11-04 07:00:00', 'Саратовская обл'),
            ('222', '7700000002', 20e6, '2026-11-03', '2026-11-03 07:00:00', 'Москва'),
            ('333', '6400000001', 40e6, '2026-09-01', '2026-09-02 07:00:00', 'Саратовская обл')]:
            self.db.conn.execute('''INSERT INTO contracts
              (reestr_number,price,advance_pct,signed_at,execution_until,first_seen_at,card_fetched_at)
              VALUES (?,?,0,?,'2027-11-01',?,'2026-11-03 07:00:00')''', (number, amount, signed, seen))
            self.db.conn.execute('INSERT OR IGNORE INTO suppliers (inn,name,email,address) VALUES (?, ?, ?, ?)',
                                 (inn, 'ООО <Тест>', 'mail@example.org', address))
            self.db.conn.execute('INSERT INTO contract_suppliers VALUES (?,?)', (number, inn))
        self.db.commit()

    def tearDown(self):
        self.state.close(); self.db.conn.close(); self.tmp.cleanup()

    def ask(self, text):
        return '\n'.join(respond(text,self.db_path,self.state,TODAY))

    def test_today_only_new_contracts(self):
        msg=self.ask('/today'); self.assertIn('новых контрактов 1', msg)
        self.assertIn('6400000001',msg); self.assertNotIn('7700000002',msg)
        self.assertIn('Сегодня база не обновлялась',msg)

    def test_leads_filters_before_largest_contract(self):
        msg=self.ask('/leads 20'); self.assertIn('6400000001', msg); self.assertIn('7700000002',msg)
        self.assertNotIn('40,0 млн',msg); self.assertIn('&lt;Тест&gt;',msg)

    def test_filters_saved_across_restart(self):
        self.ask('/filter region=64 min=5000000')
        other=State(self.state_path)
        try: self.assertEqual(other.filters(),Filters('64',5e6))
        finally: other.close()
        self.assertNotIn('7700000002',self.ask('/leads'))
        self.ask('/filter reset'); self.assertIn('7700000002',self.ask('/leads'))

    def test_text_region_casefold(self):
        self.ask('/filter регион=сАРАтов')
        self.assertIn('6400000001',self.ask('/leads')); self.assertNotIn('7700000002',self.ask('/leads'))

    def test_invalid_filters_atomic(self):
        for text in ['/filter region=77 min=nan','/filter min=inf','/filter min=-1', '/filter region=777', '/filter wrong=1', '/filter region="bad']:
            self.ask(text); self.assertEqual(self.state.filters(),Filters())

    def test_stats_global_and_week(self):
        msg=self.ask('/stats'); self.assertIn('Контрактов: 3',msg); self.assertIn('2026-11-04: 1',msg)
        self.assertIn('100.00%',msg)

    def test_interrupted_run_is_not_displayed_as_running(self):
        self.db.conn.execute('CREATE TABLE fetch_runs(id INTEGER PRIMARY KEY, status TEXT, started_at TEXT)')
        self.db.conn.execute("INSERT INTO fetch_runs(status,started_at) VALUES ('interrupted','2026-11-03 05:15:00')")
        self.db.commit()
        self.assertIn('Последний запуск сбора: прерван', self.ask('/stats'))

    def test_help_and_bad_count(self):
        self.assertIn('/filter',self.ask('/start'))
        self.assertIn('Неизвестная',self.ask('/oops'))
        self.assertIn('от 1 до 20',self.ask('/leads 999'))
        self.assertIn('Поставщики',self.ask('/leads@tender_radar_vc_bot'))

    def update(self, chat=123, sender=123, kind='private', text='/stats'):
        return {'update_id':1,'message':{'chat':{'id':chat,'type':kind},'from':{'id':sender},'text':text}}

    def test_foreign_chat_group_and_sender_ignored(self):
        calls=[]
        for u in [self.update(chat=456),self.update(sender=456),self.update(kind='group'),self.update(text=None)]:
            self.assertTrue(process_update(u, {'TELEGRAM_CHAT_ID':'123'},self.db_path,self.state,lambda *x:calls.append(x)))
        self.assertEqual(calls,[])

    def test_send_failure_does_not_confirm_update(self):
        self.assertFalse(process_update(self.update(),{'TELEGRAM_CHAT_ID':'123'},self.db_path,self.state,lambda *a:False))

    def test_digest_uses_same_filters_and_remains_available_on_request(self):
        chosen=select_leads(self.db.conn,TODAY,7,14,Filters('64'))
        self.assertEqual([l.reestr_number for l in chosen],['111'])
        self.db.conn.execute("INSERT INTO digest_sent (inn,reestr_number) VALUES ('6400000001','111')")
        self.db.commit()
        self.assertEqual(select_leads(self.db.conn,TODAY,7,14,Filters('64')),[])
        self.ask('/filter region=64'); self.assertIn('6400000001',self.ask('/leads'))

    def test_offset_survives_restart(self):
        self.state.put('offset', 42)
        other=State(self.state_path)
        try:self.assertEqual(other.get('offset'),42)
        finally:other.close()

    def test_poll_acknowledges_only_successfully_handled_updates(self):
        from unittest.mock import Mock
        api=Mock()
        api.call.return_value=[{'update_id':10}, {'update_id':11}, {'update_id':12}]
        with patch('tender_radar.bot.process_update',side_effect=[True,False]):
            with self.assertRaises(TelegramError):
                poll_once(api,{},self.db_path,self.state)
        self.assertEqual(self.state.get('offset'),11)
        api.call.return_value=[]
        poll_once(api,{},self.db_path,self.state)
        self.assertEqual(api.call.call_args[0][1]['offset'],11)

if __name__ == '__main__': unittest.main()
