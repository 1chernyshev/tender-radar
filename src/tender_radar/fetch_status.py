"""Журнал попыток сбора и защита от двух одновременных сборщиков.

После жесткого завершения running исправляется при следующем запуске,
когда файловая блокировка доказывает отсутствие другого сборщика новой версии.
Время finished_at для interrupted означает момент обнаружения, не момент сбоя.
"""
from contextlib import contextmanager
import fcntl
import sqlite3
from pathlib import Path


class FetchAlreadyRunning(RuntimeError):
    pass


def _counts(conn):
    return conn.execute('SELECT COUNT(*), COUNT(card_fetched_at) FROM contracts').fetchone()


@contextmanager
def track_fetch(db_path):
    with Path(str(db_path) + '.fetch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FetchAlreadyRunning('Другой сбор уже выполняется') from None
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS fetch_runs (
                id INTEGER PRIMARY KEY, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT, status TEXT, new_records INTEGER,
                new_cards INTEGER, reason TEXT)""")
            # Совместимость с журналом, созданным версией от 11 ноября.
            columns = {r[1] for r in conn.execute('PRAGMA table_info(fetch_runs)')}
            for name in ('baseline_records', 'baseline_cards'):
                if name not in columns:
                    conn.execute(f'ALTER TABLE fetch_runs ADD COLUMN {name} INTEGER')
            before = _counts(conn)
            # Не выдумываем точный объем данных после жесткого завершения.
            conn.execute("""UPDATE fetch_runs SET status='interrupted',
                finished_at=CURRENT_TIMESTAMP, reason='detected_on_next_start'
                WHERE status='running'""")
            run_id = conn.execute("""INSERT INTO fetch_runs
                (status, baseline_records, baseline_cards) VALUES ('running', ?, ?)""", before).lastrowid
            conn.commit()
            status, reason = 'success', ''
            try:
                yield run_id
            except BaseException as exc:
                status = 'partial' if _counts(conn) != before else 'failed'
                # Без текста исключения и внешних данных в структурированном поле.
                reason = type(exc).__name__
                raise
            finally:
                after = _counts(conn)
                conn.execute("""UPDATE fetch_runs SET finished_at=CURRENT_TIMESTAMP,
                    status=?, new_records=?, new_cards=?, reason=? WHERE id=?""",
                    (status, after[0]-before[0], after[1]-before[1], reason, run_id))
                conn.commit()
        finally:
            conn.close()
