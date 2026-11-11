"""Утренний дайджест лидов в Telegram.

После ежедневного сбора формируется сообщение с самыми перспективными
поставщиками, о которых менеджер еще не получал сведений: название, сумма,
срок, контакты, ссылка на карточку контракта. Уже отправленные лиды помечаются
в таблице digest_sent и в следующие дайджесты не попадают.

Токен и идентификатор чата берутся из файла .env. Сообщение уходит только в этот
чат. Токен входит в адрес запроса, поэтому адрес и текст исключений в журнал
не пишутся.
"""

from __future__ import annotations

import html
import fcntl
from .network import error_kind
import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from .scoring import Lead, rank

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"
TELEGRAM_LIMIT = 4000          # у Telegram предел 4096 символов, берем с запасом
CARD_URL = "https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber="

log = logging.getLogger(__name__)


class DigestDeliveryError(RuntimeError):
    """Telegram не подтвердил доставку дайджеста."""

_FORMS = (
    ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ", "ООО"),
    ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО", "ПАО"),
    ("АКЦИОНЕРНОЕ ОБЩЕСТВО", "АО"),
)


def load_env(path: Path = ENV_PATH) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def short_name(name: str, limit: int = 60) -> str:
    """Название без повторов и длинных организационно-правовых форм."""
    text = (name or "").strip()
    # ЕИС дублирует краткое название в скобках в конце: убираем дубль
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    ip = re.search(r"\s+индивидуальный предприниматель\s*$", text, flags=re.IGNORECASE)
    if ip:
        text = "ИП " + text[: ip.start()].title()
    for full, short in _FORMS:
        if text.upper().startswith(full):
            text = short + text[len(full):]
            break
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_money(price: float) -> str:
    return f"{price / 1e6:.1f}".replace(".", ",") + " млн"


def format_lead(i: int, lead: Lead, today: date) -> str:
    """Один лид в разметке HTML. Все внешние строки экранируются."""
    esc = lambda s: html.escape(s or "", quote=False)
    age = (today - lead.signed_at).days
    term = (lead.execution_until - lead.signed_at).days
    lines = [
        f"<b>{i}. {esc(short_name(lead.name))}</b> (балл {str(lead.score).replace('.', ',')})",
        f"{fmt_money(lead.price)}, срок {term} дн., подписан {age} дн. назад",
        f"ИНН {esc(lead.inn)}",
    ]
    contacts = []
    if lead.phone and any(ch.isdigit() for ch in lead.phone):
        contacts.append("тел. " + esc(lead.phone))
    if lead.email:
        contacts.append(esc(lead.email))
    if contacts:
        lines.append(", ".join(contacts))
    if lead.reestr_number.isdigit():       # в ссылку попадают только цифры
        lines.append(f'<a href="{CARD_URL}{lead.reestr_number}">карточка контракта</a>')
    return "\n".join(lines)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digest_sent (
               inn TEXT NOT NULL,
               reestr_number TEXT NOT NULL,
               sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (inn, reestr_number)
           )"""
    )


def select_leads(conn: sqlite3.Connection, today: date, top: int, fresh_days: int, filters=None) -> list[Lead]:
    """Свежие лиды с контактом, которых еще не отправляли, по убыванию балла."""
    _ensure_table(conn)
    sent = {(r[0], r[1]) for r in conn.execute("SELECT inn, reestr_number FROM digest_sent")}
    addresses = {r[0]: r[1] or '' for r in conn.execute("SELECT inn, address FROM suppliers")}
    def eligible(lead):
        return (0 <= (today - lead.signed_at).days <= fresh_days and lead.has_contact
                and (lead.inn, lead.reestr_number) not in sent
                and (filters is None or filters.matches(lead, addresses)))
    leads, _ = rank(conn, today, candidate_filter=eligible)
    return leads[:top]



def build_messages(leads: list[Lead], today: date, data_as_of: date | None = None, info: str = "") -> list[str]:
    """Текст дайджеста, разбитый на сообщения не длиннее лимита Telegram."""
    header = f"<b>Tender Radar, {today.strftime('%d.%m.%Y')}</b>"
    if data_as_of is not None and data_as_of < today:
        header += f"\nПоследняя загрузка карточек: {data_as_of.strftime('%d.%m.%Y')}. Сегодня база не обновлялась."
    if info:
        header += "\n" + html.escape(info)
    if not leads:
        return [header + "\nНовых подходящих лидов нет."]
    head = f"{header}\nНовых лидов: {len(leads)}\n"
    chunks, current = [], head
    for i, lead in enumerate(leads, 1):
        block = format_lead(i, lead, today)
        if len(current) + len(block) + 2 > TELEGRAM_LIMIT:
            chunks.append(current.rstrip())
            current = ""
        current += block + "\n\n"
    chunks.append(current.rstrip())
    return chunks


def send_message(text: str, env: dict) -> bool:
    """Отправка в личный чат. Ошибки журналируются без адреса запроса (в нем токен)."""
    token, chat_id = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("в .env нет TELEGRAM_TOKEN или TELEGRAM_CHAT_ID")
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20
            ) as resp:
                if json.load(resp).get("ok"):
                    return True
                log.error("Telegram не подтвердил отправку")
                return False
        except urllib.error.HTTPError as exc:
            log.error("Telegram HTTP %d, попытка %d/3", exc.code, attempt)
            if exc.code < 500:
                return False
        except Exception as exc:  # URL и текст исключения могут содержать токен
            reason = getattr(exc, "reason", None)
            log.error("не удалось отправить сообщение: %s, попытка %d/3", error_kind(exc), attempt)
        if attempt < 3:
            time.sleep(attempt * 2)
    return False


def _run_digest(db_path: Path, today: date, top: int = 7, fresh_days: int = 14,
               dry_run: bool = False, env: dict | None = None, retry_pending: bool = False) -> int:
    """Формирует и отправляет дайджест. Возвращает число отправленных лидов."""
    conn = sqlite3.connect(db_path)
    from .bot_state import State, STATE_PATH, Filters
    filters = Filters()
    if STATE_PATH.exists():
        state = State()
        try:
            filters = state.filters()
        finally:
            state.close()
    conn.execute("""CREATE TABLE IF NOT EXISTS digest_outbox (
        day TEXT PRIMARY KEY, messages TEXT NOT NULL, lead_keys TEXT NOT NULL,
        next_message INTEGER NOT NULL DEFAULT 0, completed_at TEXT)""")
    row = conn.execute('SELECT messages, lead_keys, next_message, completed_at FROM digest_outbox WHERE day=?', (today.isoformat(),)).fetchone()
    leads = []
    if row:
        if row[3]:
            conn.close()
            return 0
        messages, keys, next_message = json.loads(row[0]), json.loads(row[1]), row[2]
    else:
        if retry_pending:
            conn.close()
            return 0
        # Не отправлять второй дайджест после обновления старой версии программы.
        _ensure_table(conn)
        if not dry_run and conn.execute("SELECT 1 FROM digest_sent WHERE date(sent_at,'localtime')=? LIMIT 1", (today.isoformat(),)).fetchone():
            conn.close()
            return 0
        leads = select_leads(conn, today, top, fresh_days, filters)
        from .bot import data_note
        note = data_note(conn, today)
        messages = build_messages(leads, today, info=note + "\n" + filters.description())
        keys = [(lead.inn, lead.reestr_number) for lead in leads]
        next_message = 0
        if not dry_run:
            conn.execute('INSERT INTO digest_outbox(day,messages,lead_keys) VALUES (?,?,?)',
                         (today.isoformat(), json.dumps(messages, ensure_ascii=False), json.dumps(keys)))
            conn.commit()
    if dry_run:
        print("\n\n-----\n\n".join(messages))
        conn.close()
        return len(keys)
    env = env if env is not None else load_env()
    for index in range(next_message, len(messages)):
        if not send_message(messages[index], env):
            conn.close()
            raise DigestDeliveryError("Дайджест сохранен для повтора; доставка не подтверждена")
        conn.execute('UPDATE digest_outbox SET next_message=? WHERE day=?', (index+1,today.isoformat()))
        conn.commit()
    _ensure_table(conn)
    conn.executemany('INSERT OR IGNORE INTO digest_sent (inn, reestr_number) VALUES (?, ?)', keys)
    conn.execute('UPDATE digest_outbox SET completed_at=CURRENT_TIMESTAMP WHERE day=?',(today.isoformat(),))
    conn.commit()
    conn.close()
    return len(keys)


def run_digest(db_path: Path, today: date, top: int = 7, fresh_days: int = 14,
               dry_run: bool = False, env: dict | None = None, retry_pending: bool = False) -> int:
    # Один отправитель на базу, включая параллельные launchd-задачи.
    with Path(str(db_path) + '.digest.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _run_digest(db_path, today, top, fresh_days, dry_run, env, retry_pending)
