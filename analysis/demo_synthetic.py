"""Offline illustration of the real ranking code, using invented records only.

Run from the repository root: .venv/bin/python analysis/demo_synthetic.py
No EIS, Telegram, .env, working database or defense snapshot is accessed.
This demonstrates the SQL/Python ranking boundary, not HTML parser accuracy.
"""
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tender_radar.db import SCHEMA
from tender_radar.scoring import rank


def demonstrate():
    today = date(2026, 11, 16)
    with sqlite3.connect(':memory:') as conn:
        conn.executescript(SCHEMA)

        def add(number, inn, price, age, advance=0, contact=True, until=None):
            signed = today - timedelta(days=age)
            until = until or (signed + timedelta(days=180)).isoformat()
            conn.execute(
                'INSERT INTO contracts '
                '(reestr_number,status,price,advance_pct,advance_state,signed_at,execution_until) '
                'VALUES (?,?,?,?,?,?,?)',
                (number, 'Исполнение', price, advance, 'synthetic', signed.isoformat(), until),
            )
            conn.execute(
                'INSERT OR IGNORE INTO suppliers (inn,name,email) VALUES (?,?,?)',
                (inn, 'Учебная компания ' + inn[-1], 'demo@example.invalid' if contact else ''),
            )
            conn.execute('INSERT INTO contract_suppliers VALUES (?,?)', (number, inn))

        # Identifiers, names and contacts below are invented, not real leads.
        add('DEMO-A1', '0000000001', 10_000_000, 5)
        add('DEMO-A2', '0000000001', 100_000, 6)  # activity counts all appearances
        add('DEMO-B1', '0000000002', 10_000_000, 5, advance=30)
        add('DEMO-C1', '0000000003', 10_000_000, 5, advance=None)
        add('DEMO-D-OLD', '0000000004', 50_000_000, 70)
        add('DEMO-D-FRESH', '0000000004', 2_000_000, 2)
        add('DEMO-E1', '0000000005', 10_000_000, 5, contact=False)
        add('DEMO-F1', '0000000006', 10_000_000, 5, until='invalid-date')
        conn.commit()

        candidates, dropped = rank(
            conn, today,
            candidate_filter=lambda lead: lead.has_contact
            and 0 <= (today - lead.signed_at).days <= 14,
        )
        actual = [(lead.reestr_number, lead.score) for lead in candidates]
        expected = [('DEMO-A1', 67.3), ('DEMO-D-FRESH', 50.3)]
        if actual != expected:
            raise RuntimeError(f'Demo changed: {actual!r}, expected {expected!r}')
        return {
            'kind': 'synthetic illustration, not market validation',
            'as_of': today.isoformat(),
            'records': 8,
            'expected_result_matches': True,
            'leads': [
                {'contract': lead.reestr_number, 'score': lead.score,
                 'parts': {key: round(value, 3) for key, value in lead.parts.items()}}
                for lead in candidates
            ],
            'python_exclusion_counts': dict(dropped),
            'explanation': [
                'Positive and unknown advances are excluded by SQL.',
                'Invalid dates are excluded in Python.',
                'Missing contact and age over 14 days are excluded by the predicate.',
                'The predicate runs before choosing the largest contract per supplier.',
                'Activity counts all supplier appearances, including a small contract.',
                'No claim about parser accuracy, actual credit demand or conversion.',
            ],
        }


if __name__ == '__main__':
    print(json.dumps(demonstrate(), ensure_ascii=False, indent=2))
