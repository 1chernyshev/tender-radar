"""Личный Telegram-бот: команды по локальной базе, long polling без доступа к ЕИС."""
from __future__ import annotations
import fcntl
import html
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from .bot_state import State, parse_filters
from .digest import ROOT, load_env, send_message, format_lead
from .scoring import rank
from .network import error_kind

log = logging.getLogger(__name__)
HELP = ('<b>Tender Radar</b>\n'
        '/today — сбор за сегодня и подходящие новые поставщики\n'
        '/leads — топ-7 поставщиков за последние 14 дней; /leads 20 — до 20\n'
        '/stats — база и сбор за последние 7 дней\n'
        '/filter — настройки; пример: /filter регион=Саратов сумма=5000000\n'
        '/filter region=64 min=5000000 — регион по первым цифрам ИНН\n'
        '/filter reset — сброс\n'
        'Для названия с пробелом: регион="Московская обл". Фильтры действуют на списки и утренний дайджест.\n'
        'Регион относится к регистрации поставщика, а не месту исполнения контракта.')
COMMANDS = [dict(command=c, description=d) for c, d in [
    ('today', 'Сбор за сегодня и новые поставщики'), ('leads', 'Рейтинг свежих поставщиков'),
    ('stats', 'Статистика базы и сбора'), ('filter', 'Регион и минимальная сумма'), ('help', 'Как пользоваться')]]

class TelegramError(RuntimeError):
    pass

class Telegram:
    def __init__(self, env):
        self.token = env['TELEGRAM_TOKEN']

    def call(self, method, params=None):
        data = urllib.parse.urlencode(params or {}).encode()
        try:
            with urllib.request.urlopen(f'https://api.telegram.org/bot{self.token}/{method}', data=data, timeout=40) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise TelegramError(f'HTTP {exc.code}') from None
        except Exception as exc:
            raise TelegramError(error_kind(exc)) from None
        if not result.get('ok'):
            raise TelegramError('Telegram не подтвердил запрос')
        return result['result']


def read_db(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def data_note(conn, today):
    last = conn.execute("SELECT date(MAX(card_fetched_at), 'localtime') FROM contracts").fetchone()[0]
    note = 'Карточки еще не загружены.' if not last else f'Последняя загрузка карточек: {date.fromisoformat(last):%d.%m.%Y}.'
    if last and last < today.isoformat():
        note += ' Сегодня база не обновлялась.'
    try:
        run = conn.execute("SELECT status, started_at FROM fetch_runs ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        run = None
    if run:
        labels = {'running': 'выполняется', 'success': 'завершен', 'partial': 'частичный', 'failed': 'ошибка', 'interrupted': 'прерван (выявлено при следующем запуске)'}
        note += f' Последний запуск сбора: {labels.get(run[0], run[0])} ({run[1]} UTC).'
    return note


def ranked(conn, today, filters, only_today=False):
    addresses = {r['inn']: r['address'] or '' for r in conn.execute('SELECT inn, address FROM suppliers')}
    new_contracts = {r[0] for r in conn.execute(
        "SELECT reestr_number FROM contracts WHERE date(first_seen_at, 'localtime')=?", (today.isoformat(),))} if only_today else None
    def eligible(lead):
        age = (today - lead.signed_at).days
        return (0 <= age <= 14 and lead.has_contact and filters.matches(lead, addresses)
                and (new_contracts is None or lead.reestr_number in new_contracts))
    return rank(conn, today, candidate_filter=eligible)[0]


def lead_messages(title, leads, today, note, filters):
    current = f'<b>{title}</b>\n{note}\n{html.escape(filters.description())}\n'
    messages = []
    if not leads:
        return [current + 'Подходящих поставщиков нет. Можно изменить /filter.']
    for i, lead in enumerate(leads, 1):
        safe = replace(lead, phone=lead.phone[:150], email=lead.email[:150])
        block = '\n' + format_lead(i, safe, today) + '\n'
        if len(current) + len(block) > 3800:
            messages.append(current)
            current = ''
        current += block
    messages.append(current)
    return messages


def respond(text, db_path, state, today=None):
    today = today or date.today()
    command, _, args = text.strip().partition(' ')
    command = command.split('@')[0].lower()
    if command in ('/start', '/help'):
        return [HELP]
    if command == '/filter':
        try:
            filters = parse_filters(args, state.filters()) if args.strip() else state.filters()
        except ValueError as exc:
            return [html.escape(str(exc)) + '\nПример: /filter region=64 min=5000000\nСброс: /filter reset']
        if args.strip():
            state.put('filters', vars(filters))
        return [html.escape(filters.description()) + '\nПрименяется к /today, /leads и дайджесту.\n'
                'Текст региона ищется в адресе поставщика; код — первые две цифры ИНН.\n'
                'Пример: /filter region=64 min=5000000\nСброс: /filter reset']
    if command not in ('/today', '/leads', '/stats'):
        return ['Неизвестная команда. Список: /help']
    conn = read_db(db_path)
    try:
        filters = state.filters()
        note = data_note(conn, today)
        if command in ('/today', '/leads'):
            top = 7
            if args.strip():
                if command != '/leads' or not args.strip().isdigit() or not 1 <= int(args) <= 20:
                    return ['Используйте /today или /leads [число от 1 до 20].']
                top = int(args)
            leads = ranked(conn, today, filters, command == '/today')
            title = f'Поставщики: топ-{min(top, len(leads))} из {len(leads)}'
            if command == '/today':
                count = conn.execute("SELECT COUNT(*) FROM contracts WHERE date(first_seen_at, 'localtime')=?", (today.isoformat(),)).fetchone()[0]
                title = f'Сегодня {today:%d.%m.%Y}: новых контрактов {count}, подходящих поставщиков {len(leads)}'
            return lead_messages(title, leads[:top], today, note, filters)
        total, cards, known, zero = conn.execute('SELECT COUNT(*), SUM(card_fetched_at IS NOT NULL), SUM(advance_pct IS NOT NULL), SUM(advance_pct=0) FROM contracts').fetchone()
        suppliers, mail = conn.execute("SELECT COUNT(*), SUM(COALESCE(email,'')!='') FROM suppliers").fetchone()
        since = today - timedelta(days=6)
        week = conn.execute("SELECT date(first_seen_at,'localtime') AS day, COUNT(*) AS n FROM contracts WHERE date(first_seen_at,'localtime') BETWEEN ? AND ? GROUP BY day ORDER BY day", (since.isoformat(), today.isoformat())).fetchall()
        pct = f'{100 * (zero or 0) / known:.2f}%' if known else 'нет данных'
        return [f'<b>База на {today:%d.%m.%Y}</b>\n{note}\nКонтрактов: {total}; с карточками: {cards or 0}\n'
                f'Поставщиков: {suppliers}; с почтой: {mail or 0}\nБез аванса: {pct} среди известных значений\n'
                f'Новые контракты за 7 дней (по дате попадания в базу):\n' +
                ('\n'.join(f'{r["day"]}: {r["n"]}' for r in week) or 'Нет новых записей.') +
                '\nСтатистика по всей базе; фильтры не применяются. Это выборка, не весь рынок.']
    finally:
        conn.close()


def process_update(update, env, db_path, state, sender=send_message):
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    owner = str(env['TELEGRAM_CHAT_ID'])
    if (chat.get('type') != 'private' or str(chat.get('id')) != owner
            or str((message.get('from') or {}).get('id')) != owner):
        return True
    text = message.get('text')
    if not isinstance(text, str):
        return True
    try:
        messages = respond(text, db_path, state)
    except Exception as exc:
        log.error('ошибка команды: %s', type(exc).__name__)
        messages = ['Не удалось прочитать базу. Попробуйте позже.']
    return all(sender(part, env) for part in messages)


def poll_once(api, env, db_path, state):
    updates = api.call('getUpdates', {'offset': state.get('offset', 0), 'timeout': 25,
                                     'allowed_updates': json.dumps(['message'])})
    for update in updates:
        if not process_update(update, env, db_path, state):
            raise TelegramError('ответ не доставлен')
        state.put('offset', update['update_id'] + 1)


def poll_forever(api, env, db_path, state):
    delay, failures, outage_since = 2, 0, None
    while True:
        try:
            poll_once(api, env, db_path, state)
        except TelegramError as exc:
            if outage_since is None:
                # Время по часам включает сон Mac, в отличие от monotonic на macOS.
                outage_since = time.time()
            failures += 1
            log.warning('Telegram: %s; сбой подряд %d; повтор через %d с', exc, failures, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        else:
            if failures:
                log.info('Telegram: опрос восстановлен после %d сбоев, %.0f с с первой ошибки',
                         failures, max(0, time.time() - outage_since))
            delay, failures, outage_since = 2, 0, None


def run_bot(db_path, state_path=None):
    env = load_env()
    if not env.get('TELEGRAM_TOKEN') or not env.get('TELEGRAM_CHAT_ID', '').isdigit():
        raise SystemExit('Нужны TELEGRAM_TOKEN и положительный TELEGRAM_CHAT_ID личного чата в .env')
    lock_path = ROOT / 'data' / 'bot.lock'
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Обработчик бота уже запущен')
        state = State(state_path) if state_path else State()
        api = Telegram(env)
        try:
            info = api.call('getWebhookInfo')
            if info.get('url'):
                raise SystemExit('Настроен webhook; остановка без изменения существующей настройки')
            username = api.call('getMe')['username']
            api.call('setMyCommands', {'commands': json.dumps(COMMANDS, ensure_ascii=False),
                     'scope': json.dumps({'type': 'chat', 'chat_id': int(env['TELEGRAM_CHAT_ID'])})})
            log.info('обработчик @%s запущен; доступ только владельцу', username)
            poll_forever(api, env, db_path, state)
        finally:
            state.close()
