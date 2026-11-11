import sys,tempfile,sqlite3,unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tender_radar import digest,cli
from tender_radar.db import Database
from tender_radar.network import error_kind
import socket,urllib.error
D=date(2026,11,11)
class QueueTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'t.db';self.db=Database(self.path)
  self.db.conn.execute("INSERT INTO contracts (reestr_number,price,advance_pct,signed_at,execution_until) VALUES ('111',20000000,0,'2026-11-10','2027-11-10')")
  self.db.conn.execute("INSERT INTO suppliers (inn,name,email) VALUES ('6400000001','ООО Тест','a@example.org')")
  self.db.conn.execute("INSERT INTO contract_suppliers VALUES ('111','6400000001')");self.db.commit()
 def tearDown(self):self.db.conn.close();self.tmp.cleanup()
 def test_failed_send_then_retry_then_no_duplicate(self):
  with patch.object(digest,'send_message',return_value=False):
   with self.assertRaises(digest.DigestDeliveryError):digest.run_digest(self.path,D,env={})
  self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM digest_sent').fetchone()[0],0)
  with patch.object(digest,'send_message',return_value=True) as send:
   self.assertEqual(digest.run_digest(self.path,D,env={},retry_pending=True),1)
   self.assertEqual(digest.run_digest(self.path,D,env={}),0);self.assertEqual(send.call_count,1)
 def test_retry_does_not_create_new_digest(self):
  with patch.object(digest,'send_message') as send:
   self.assertEqual(digest.run_digest(self.path,D,env={},retry_pending=True),0);send.assert_not_called()
 def test_confirmed_chunk_not_repeated(self):
  with patch.object(digest,'build_messages',return_value=['first','second']),patch.object(digest,'send_message',side_effect=[True,False]):
   with self.assertRaises(digest.DigestDeliveryError):digest.run_digest(self.path,D,env={})
  with patch.object(digest,'send_message',return_value=True) as send:
   digest.run_digest(self.path,D,env={},retry_pending=True)
   self.assertEqual([c.args[0] for c in send.call_args_list],['second'])
 def test_partial_fetch_logged_and_nonzero(self):
  def fail(args):
   self.db.conn.execute("INSERT INTO contracts(reestr_number) VALUES ('222')");self.db.commit();raise RuntimeError('offline')
  with patch.object(cli,'DB_PATH',self.path),patch.object(cli,'_fetch',side_effect=fail):
   with self.assertRaises(SystemExit):cli.cmd_fetch(None)
  self.assertEqual(tuple(self.db.conn.execute('SELECT status,new_records FROM fetch_runs').fetchone()),('partial',1))
 def test_empty_first_page_fails_without_backfill(self):
  import argparse
  args=argparse.Namespace(delay=0,keep_raw=False,date_to=None,date_from=None,days=1,pages=2,cards=True)
  with patch.object(cli,'DB_PATH',self.path),patch.object(cli,'EisClient') as client,patch.object(cli,'parse_contract_search',return_value=[]),patch.object(cli.time,'sleep'):
   client.return_value.get.return_value='empty'
   with self.assertRaises(SystemExit) as result:cli.cmd_fetch(args)
   self.assertEqual(result.exception.code,2)
  self.assertEqual(self.db.conn.execute('SELECT status FROM fetch_runs').fetchone()[0],'failed')
 def test_network_reason_without_secret(self):
  self.assertEqual(error_kind(urllib.error.URLError(socket.gaierror('secret'))),'DNS')
  self.assertEqual(error_kind(urllib.error.URLError(TimeoutError('secret'))),'timeout')
if __name__=='__main__':unittest.main()
