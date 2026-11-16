"""Command-line entry point: `python -m tender_radar fetch|stats`."""

from __future__ import annotations

import argparse
import logging
import random
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from .client import BlockedError, EisClient
from .db import Database
from .parsers import parse_contract_card, parse_contract_search, parse_total_records

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "tender_radar.db"
RAW_DIR = ROOT / "data" / "raw"

SEARCH_PATH = "/epz/contract/search/results.html"
CARD_PATH = "/epz/contract/contractCard/common-info.html"


def search_params(page: int, date_from: date, date_to: date, per_page: int = 50) -> dict:
    return {
        "searchString": "",
        "morphology": "on",
        "fz44": "on",
        "contractStageList_0": "on",   # Заключение
        "contractStageList_1": "on",   # Исполнение
        "contractStageList": "0,1",
        "publishDateFrom": date_from.strftime("%d.%m.%Y"),
        "publishDateTo": date_to.strftime("%d.%m.%Y"),
        "pageNumber": page,
        "sortDirection": "false",
        "recordsPerPage": f"_{per_page}",
        "sortBy": "UPDATE_DATE",
        "currencyIdGeneral": "-1",
    }


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%d.%m.%Y").date()


def cmd_fetch(args: argparse.Namespace) -> None:
    from .fetch_status import FetchAlreadyRunning, track_fetch
    Database(DB_PATH).conn.close()
    try:
        with track_fetch(DB_PATH):
            _fetch(args)
    except FetchAlreadyRunning as exc:
        logging.error("%s", exc)
        raise SystemExit(3)
    except (BlockedError, RuntimeError) as exc:
        logging.error("Сбор не завершен: %s", exc)
        raise SystemExit(2)


def _fetch(args: argparse.Namespace) -> None:
    client = EisClient(delay=args.delay, raw_dir=RAW_DIR if args.keep_raw else None)
    # При отказе HTTP закрываем пишущее соединение до финализации fetch_runs.
    # Незавершенный upsert откатывается; ранее зафиксированные карточки остаются.
    with closing(client.session):
        db = Database(DB_PATH)
        with closing(db.conn):
            _fetch_pages(args, client, db)


def _fetch_pages(args: argparse.Namespace, client: EisClient, db: Database) -> None:
    date_to = _parse_date(args.date_to) if args.date_to else date.today()
    date_from = _parse_date(args.date_from) if args.date_from else date_to - timedelta(days=args.days)
    found = new_cards = 0

    def load_page(page: int, retry_empty: bool) -> list[dict]:
        """Страница выдачи; пустой ответ на первой странице — повод повторить."""
        attempts = 3 if retry_empty else 1
        rows: list[dict] = []
        for attempt in range(1, attempts + 1):
            html = client.get(SEARCH_PATH, search_params(page, date_from, date_to),
                              save_as=f"search_p{page}.html")
            rows = parse_contract_search(html)
            if attempt == 1:
                load_page.last_html = html
            if rows or attempt == attempts:
                break
            logging.warning("страница %d: пусто, повтор %d/%d", page, attempt, attempts - 1)
            time.sleep(args.delay * 5 * attempt)
        return rows

    # Первая страница нужна всегда: по ней видно, сколько всего записей в выдаче.
    first_rows = load_page(1, retry_empty=True)
    if not first_rows:
        logging.error("поиск за %s..%s вернул пусто", date_from, date_to)
        db.log_fetch(args.pages, 0, 0, f"{date_from}..{date_to} пусто")
        db.commit()
        raise RuntimeError("Пустая первая выдача ЕИС после повторов")
    total, exact = parse_total_records(load_page.last_html)
    per_page = 50
    max_page = max(1, (total + per_page - 1) // per_page) if total else args.pages
    logging.info("в выдаче %s%s записей, страниц ~%d",
                 "" if exact else "более ", total, max_page)

    # Случайная выборка страниц вместо первых подряд: первые страницы отсортированы
    # по дате обновления и не представляют выдачу целиком. Зерно — дата периода,
    # чтобы повторный запуск за тот же день брал те же страницы.
    rng = random.Random(f"{date_from}..{date_to}")
    pool = [p for p in range(2, max_page + 1)]
    rng.shuffle(pool)
    pages = [1] + pool[: max(0, args.pages - 1)]
    logging.info("отобраны страницы: %s", pages)

    for page in pages:
        rows = first_rows if page == 1 else load_page(page, retry_empty=False)
        logging.info("страница %d: %d контрактов", page, len(rows))
        if not rows:
            raise RuntimeError("Пустая страница выбранной выдачи ЕИС")
        for row in rows:
            found += 1
            db.upsert_summary(row)
            if args.cards and db.needs_card(row["reestr_number"]):
                card_html = client.get(
                    CARD_PATH, {"reestrNumber": row["reestr_number"]},
                    save_as=f"card_{row['reestr_number']}.html",
                )
                card = parse_contract_card(card_html)
                from .maintenance import validate_card
                validate_card(card, row["reestr_number"])
                db.save_card(card)
                new_cards += 1
                logging.info("  card %s: %s | %s | %.2f | advance %s",
                             row["reestr_number"],
                             card["suppliers"][0]["name"][:50] if card["suppliers"] else "-",
                             card["suppliers"][0]["inn"] if card["suppliers"] else "-",
                             card["price"] or 0, card["advance_pct"])
            db.commit()
    db.log_fetch(args.pages, found, new_cards,
                 f"{date_from}..{date_to} страницы {pages} из ~{max_page}")
    db.commit()
    logging.info("done: found %d, new cards %d; stats %s", found, new_cards, db.stats())


def cmd_backfill(args: argparse.Namespace) -> None:
    """Добор контактов поставщиков, собранных до исправления разбора колонок.

    Контракты не трогаем, обновляем только карточку поставщика. Сначала идут
    те, у кого самый крупный контракт: именно они интересны банку.
    """
    try:
        _backfill(args)
    except BlockedError as exc:
        logging.error("%s. Следующая попытка - при следующем запуске.", exc)
        raise SystemExit(2)
    except RuntimeError as exc:
        logging.error("ЕИС недоступен: %s. Добор контактов остановлен.", exc)
        raise SystemExit(2)


def _backfill(args: argparse.Namespace) -> None:
    from .maintenance import maintain_cards
    client = EisClient(delay=args.delay, raw_dir=RAW_DIR if args.keep_raw else None)
    db = Database(DB_PATH)
    try:
        maintain_cards(db, client, args.limit, getattr(args, 'refresh_limit', 0),
                       getattr(args, 'refresh_days', 7))
    finally:
        db.conn.close()
        client.session.close()


def _csv_safe(value):
    """Экранирует ячейку, которую Excel может принять за формулу (CSV-инъекция)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def cmd_leads(args: argparse.Namespace) -> None:
    """Ранжированный список поставщиков по правилам приоритизации, в CSV."""
    import csv
    import sqlite3

    from .scoring import rank

    today = _parse_date(args.date) if args.date else date.today()
    conn = sqlite3.connect(DB_PATH)
    def eligible(lead):
        return ((args.fresh_days is None or 0 <= (today - lead.signed_at).days <= args.fresh_days)
                and (not args.with_contact or lead.has_contact))
    leads, dropped = rank(conn, today, candidate_filter=eligible)
    conn.close()
    out = ROOT / "data" / f"leads_{today.isoformat()}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["место", "балл", "поставщик", "ИНН", "цена контракта", "подписан",
                    "исполнение до", "контрактов в выборке", "телефон", "почта",
                    "реестровый номер", "сумма", "срок", "свежесть", "активность"])
        for i, l in enumerate(leads[: args.top], 1):
            row = [i, l.score, l.name, l.inn, round(l.price), l.signed_at, l.execution_until,
                   l.contracts_in_sample, l.phone, l.email, l.reestr_number,
                   *(round(l.parts[k], 1) for k in ("size", "duration", "freshness", "activity"))]
            w.writerow([_csv_safe(v) for v in row])
    logging.info("кандидатов %d, отсеяно %s, в файл %d -> %s",
                 len(leads), dict(dropped), min(len(leads), args.top), out)


def cmd_digest(args: argparse.Namespace) -> None:
    """Дайджест свежих лидов в Telegram (только в личный чат из .env)."""
    from .digest import DigestDeliveryError, run_digest

    today = _parse_date(args.date) if args.date else date.today()
    try:
        sent = run_digest(DB_PATH, today, top=args.top, fresh_days=args.fresh_days, dry_run=args.dry_run, retry_pending=args.retry_pending)
    except DigestDeliveryError as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
    logging.info("дайджест: лидов %d%s", sent, " (пробный запуск, ничего не отправлено)" if args.dry_run else "")


def cmd_bot(args: argparse.Namespace) -> None:
    from .bot import run_bot
    run_bot(DB_PATH)


def cmd_stats(args: argparse.Namespace) -> None:
    db = Database(DB_PATH)
    for k, v in db.stats().items():
        print(f"{k:12} {v}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tender_radar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch recent contracts from the ЕИС registry")
    f.add_argument("--pages", type=int, default=1)
    f.add_argument("--days", type=int, default=1, help="lookback window for publish date")
    f.add_argument("--date-from", help="начало периода публикации, ДД.ММ.ГГГГ (для добора пропусков)")
    f.add_argument("--date-to", help="конец периода публикации, ДД.ММ.ГГГГ")
    f.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    f.add_argument("--cards", action="store_true", help="also fetch contract cards (suppliers)")
    f.add_argument("--keep-raw", action="store_true", help="save raw HTML to data/raw")
    f.set_defaults(func=cmd_fetch)
    b = sub.add_parser("backfill-contacts", help="добрать контакты у ранее собранных поставщиков")
    b.add_argument("--limit", type=int, choices=range(0, 21), default=20, help="общий лимит загрузок карточек, максимум 20")
    b.add_argument("--refresh-limit", type=int, default=0, help="часть общего лимита на обновление известных карточек")
    b.add_argument("--refresh-days", type=int, default=7, help="возраст карточки для обновления, дней")
    b.add_argument("--delay", type=float, default=5.0)
    b.add_argument("--keep-raw", action="store_true")
    b.set_defaults(func=cmd_backfill)
    l = sub.add_parser("leads", help="ранжированный список поставщиков в CSV")
    l.add_argument("--top", type=int, default=100)
    l.add_argument("--fresh-days", type=int, help="только контракты, подписанные не раньше N дней назад")
    l.add_argument("--with-contact", action="store_true", help="только с телефоном или почтой")
    l.add_argument("--date", help="дата расчета, ДД.ММ.ГГГГ (по умолчанию сегодня)")
    l.set_defaults(func=cmd_leads)
    g = sub.add_parser("digest", help="отправить дайджест лидов в Telegram")
    g.add_argument("--top", type=int, default=7)
    g.add_argument("--fresh-days", type=int, default=14)
    g.add_argument("--date", help="дата расчета, ДД.ММ.ГГГГ")
    g.add_argument("--dry-run", action="store_true", help="показать текст, не отправляя")
    g.add_argument("--retry-pending", action="store_true", help="повторить только сохраненный недоставленный дайджест за сегодня")
    g.set_defaults(func=cmd_digest)
    bot = sub.add_parser("bot", help="личный Telegram-бот с командами")
    bot.set_defaults(func=cmd_bot)
    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
