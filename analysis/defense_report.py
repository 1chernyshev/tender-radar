"""Воспроизводимый срез: SQLite backup, агрегаты, проверки качества и скоринга.
Запуск: .venv/bin/python analysis/defense_report.py --db data/snapshots/defense-2026-11-11.db --out analysis/defense-2026-11-11.json
Первичное создание копии выполняется отдельно через sqlite3.Connection.backup.
"""
import argparse,sqlite3,json,hashlib,sys,statistics,random
from datetime import date
from pathlib import Path
from collections import Counter,defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tender_radar.scoring import rank,WEIGHTS,score_lead

def analyze(path,as_of):
 c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
 rows=[dict(r) for r in c.execute('SELECT * FROM contracts')]
 suppliers=[dict(r) for r in c.execute('SELECT * FROM suppliers')]
 known=[r for r in rows if r['advance_pct'] is not None];zero=[r for r in known if r['advance_pct']==0]
 segment=[r for r in zero if (r['price'] or 0)>=1e6]
 groups=defaultdict(list);old=Counter();types=Counter()
 def classify(s):
  if 'поставк' in s:return 'товары'
  if any(k in s for k in ['выполнен','ремонт','строител','работ']):return 'работы'
  if any(k in s for k in ['оказан','услуг']):return 'услуги'
  return 'не определено'
 for r in segment:
  types[classify((r['subject'] or '').casefold())]+=1
  old[classify(c.execute('SELECT lower(?)',(r['subject'] or '',)).fetchone()[0])]+=1
 for r in known:
  if r['published_at']:groups[date.fromisoformat(r['published_at']).strftime('%Y-W%W')].append(r)
 leads,dropped=rank(c,as_of)
 fresh,_=rank(c,as_of,candidate_filter=lambda l:0<=(as_of-l.signed_at).days<=14 and l.has_contact)
 base={l.inn for l in leads[:50]};rng=random.Random(20261111);overlaps=[]
 for _ in range(200):
  weights={k:v*rng.uniform(.5,1.5) for k,v in WEIGHTS.items()};total=sum(weights.values());weights={k:100*v/total for k,v in weights.items()}
  altered=sorted(leads,key=lambda l:score_lead(l,as_of,weights).score,reverse=True)
  overlaps.append(len(base&{l.inn for l in altered[:50]})/max(1,len(base)))
 for l in leads:score_lead(l,as_of)
 results={
  'as_of':as_of.isoformat(),'snapshot_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
  'integrity':c.execute('PRAGMA quick_check').fetchone()[0],
  'contracts':len(rows),'cards':sum(bool(r['card_fetched_at']) for r in rows),
  'period':[min(r['published_at'] for r in rows if r['published_at']),max(r['published_at'] for r in rows if r['published_at'])],
  'suppliers':len(suppliers),'with_email':sum(bool(r['email']) for r in suppliers),
  'known_advance':len(known),'zero_advance':len(zero),'zero_advance_pct':round(100*len(zero)/len(known),2),
  'total_price':sum(r['price'] or 0 for r in rows),
  'segment_contracts':len(segment),'segment_share_zero_count_pct':round(100*len(segment)/len(zero),2),
  'segment_share_zero_sum_pct':round(100*sum(r['price'] for r in segment)/sum(r['price'] or 0 for r in zero),2),
  'weekly':[{'week':w,'n':len(rs),'zero_pct':round(100*sum(r['advance_pct']==0 for r in rs)/len(rs),2)} for w,rs in sorted(groups.items())],
  'subject_types':dict(types),'subject_types_sql_lower':dict(old),
  'score_candidates':len(leads),'fresh_contact_candidates':len(fresh),'dropped':dict(dropped),
  'top50_sensitivity_median_pct':round(statistics.median(overlaps)*100,1),'top50_sensitivity_min_pct':round(min(overlaps)*100,1),
  'score_median':round(statistics.median(l.score for l in leads),1),
  'missing_dates':sum(not r['signed_at'] or not r['execution_until'] for r in rows),
  'invalid_terms':sum(bool(r['signed_at'] and r['execution_until'] and r['execution_until']<r['signed_at']) for r in rows),
  'weights':WEIGHTS,
  'registration_region_top':Counter(r['inn'][:2] for r in suppliers).most_common(5),
  'limitations':['Неполная страничная выборка, первая страница включается всегда; размер выборки менялся.',
   'Нет данных об обращениях за кредитом: отсутствие аванса не доказывает кассовый разрыв.',
   'Скоринг экспертный, не вероятность выдачи кредита; устойчивость не равна бизнес-качеству.',
   'Тип предмета определяется ключевыми словами, регион по ИНН относится к регистрации.',
   'Порог 8 контрактов зависит от длины наблюдений; карточки уже известных контрактов не обновляются.']}
 c.close();return results
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--out',required=True);p.add_argument('--as-of',default='2026-11-11');a=p.parse_args()
 Path(a.out).write_text(json.dumps(analyze(a.db,date.fromisoformat(a.as_of)),ensure_ascii=False,indent=2)+'\n')
 print(a.out)
