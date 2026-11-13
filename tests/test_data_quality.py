"""Аванс и даты: реальные fixture HTML, пограничные случаи и миграция БД."""
import sys,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import date
from bs4 import BeautifulSoup
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tender_radar.parsers import parse_contract_card,_advance_pct,_date
from tender_radar.db import Database,SCHEMA
from tender_radar.scoring import rank,freshness_part
FIXTURE=Path(__file__).parent/'fixtures/contract_card.html'
TODAY=date(2026,11,12)

def card_with_advance(value, remove=False, no_info=False):
    soup=BeautifulSoup(FIXTURE.read_text(),'lxml')
    label=next(x for x in soup.select('span.section__title') if x.get_text(strip=True).startswith('Размер аванса'))
    section=label.find_parent('section')
    if remove:section.decompose()
    elif no_info:section.select_one('span.section__info').decompose()
    else:
        info=section.select_one('span.section__info');info.clear();info.append(value)
    return parse_contract_card(str(soup))

class AdvanceTests(unittest.TestCase):
    def test_explicit_zero(self):
        card=card_with_advance('0 %');self.assertEqual((card['advance_pct'],card['advance_state']),(0,'explicit'))
    def test_explicit_decimal_and_amount(self):
        card=card_with_advance('30,5%, что составляет 100 000,00 ₽')
        self.assertEqual((card['advance_pct'],card['advance_state']),(30.5,'explicit'))
    def test_empty_is_unknown_not_zero(self):
        card=card_with_advance('');self.assertIsNone(card['advance_pct']);self.assertEqual(card['advance_state'],'empty')
    def test_title_without_value_is_unknown_not_zero(self):
        card=card_with_advance('',no_info=True);self.assertIsNone(card['advance_pct']);self.assertEqual(card['advance_state'],'empty')
    def test_absent_on_complete_card_is_marked_assumption(self):
        card=card_with_advance('',remove=True);self.assertEqual((card['advance_pct'],card['advance_state']),(0,'inferred_zero'))
    def test_unparsed_amount_is_not_zero(self):
        card=card_with_advance('100 000 рублей');self.assertIsNone(card['advance_pct']);self.assertEqual(card['advance_state'],'unparsed')
    def test_incomplete_html_is_not_inferred_zero(self):
        card=parse_contract_card('<section class="section"><span class="section__title">Статус контракта</span><span class="section__info">Исполнение</span></section>')
        self.assertIsNone(card['advance_pct']);self.assertEqual(card['advance_state'],'unknown')
    def test_invalid_and_ambiguous_percentages(self):
        for value in ['-5%','−1 %','101%','до 30%','от 10%','10–30%','10 % или 20 %','не определено']:
            with self.subTest(value=value):self.assertIsNone(_advance_pct(value))
    def test_limits(self):
        self.assertEqual(_advance_pct('100%'),100);self.assertEqual(_advance_pct('0%'),0)
    def test_bad_calendar_date_does_not_crash(self):
        self.assertIsNone(_date('31.02.2026'));self.assertIsNone(_date('нет даты'))
        self.assertEqual(_date('12.11.2026'),TODAY)
    def test_state_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'t.db');card=card_with_advance('0%')
            db.conn.execute('INSERT INTO contracts (reestr_number) VALUES (?)',(card['reestr_number'],))
            db.save_card(card);db.commit()
            self.assertEqual(tuple(db.conn.execute('SELECT advance_pct,advance_state FROM contracts').fetchone()),(0,'explicit'));db.conn.close()
    def test_old_schema_is_migrated_without_reclassifying_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'t.db';c=sqlite3.connect(path);c.executescript(SCHEMA.replace('    advance_state      TEXT,\n',''))
            c.execute("INSERT INTO contracts(reestr_number,advance_pct) VALUES ('1',0)");c.commit();c.close()
            db=Database(path)
            self.assertEqual(tuple(db.conn.execute('SELECT advance_pct,advance_state FROM contracts').fetchone()),(0,None));db.conn.close()

class DateScoringTests(unittest.TestCase):
    def candidates(self,signed,until,advance=0):
        with tempfile.TemporaryDirectory() as tmp:
            db=Database(Path(tmp)/'t.db')
            db.conn.execute('INSERT INTO contracts (reestr_number,price,advance_pct,signed_at,execution_until) VALUES (\'1\',5000000,?,?,?)',(advance,signed,until))
            db.conn.execute("INSERT INTO suppliers (inn,name,email) VALUES ('6400000001','ООО Тест','a@example.org')")
            db.conn.execute("INSERT INTO contract_suppliers VALUES ('1','6400000001')");db.commit()
            result=rank(db.conn,TODAY);db.conn.close();return result
    def test_valid_contract_kept(self):self.assertEqual(len(self.candidates('2026-11-10','2027-01-01')[0]),1)
    def test_reversed_term_excluded_with_reason(self):
        leads,dropped=self.candidates('2026-11-10','2026-11-09');self.assertEqual(leads,[]);self.assertEqual(dropped['окончание раньше подписания'],1)
    def test_future_signing_excluded(self):
        leads,dropped=self.candidates('2026-11-13','2027-01-01');self.assertEqual(leads,[]);self.assertEqual(dropped['дата подписания в будущем'],1)
    def test_missing_and_malformed_dates_excluded(self):
        for signed,until in [(None,'2027-01-01'),('bad','2027-01-01'),('2026-11-10','2026-02-31')]:
            leads,dropped=self.candidates(signed,until);self.assertEqual(leads,[]);self.assertEqual(dropped['неизвестная или неверная дата'],1)
    def test_unknown_advance_is_excluded(self):self.assertEqual(self.candidates('2026-11-10','2027-01-01',None)[0],[])
    def test_future_freshness_never_gets_full_points(self):self.assertEqual(freshness_part(date(2026,11,13),TODAY),0)
    def test_same_day_and_today_deadline_allowed(self):self.assertEqual(len(self.candidates('2026-11-12','2026-11-12')[0]),1)

if __name__=='__main__':unittest.main()
