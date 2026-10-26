"""Command-line entry point: `python -m tender_radar fetch|stats`."""

from __future__ import annotations

import argparse
import logging
import random
import time
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
    try:
        _fetch(args)
    except BlockedError as exc:
        logging.error("%s. Следующая попытка — при следующем запуске.", exc)
        raise SystemExit(0)
    except RuntimeError as exc:
        # Сайт недоступен (техработы, таймауты): данные за день доберутся позже.
        logging.error("ЕИС недоступен: %s. Следующая попытка — при следующем запуске.", exc)
        raise SystemExit(0)


def _fetch(args: argparse.Namespace) -> None:
    client = EisClient(delay=args.delay, raw_dir=RAW_DIR if args.keep_raw else None)
    db = Database(DB_PATH)
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
        return
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
            continue
        for row in rows:
            found += 1
            db.upsert_summary(row)
            if args.cards and db.needs_card(row["reestr_number"]):
                card_html = client.get(
                    CARD_PATH, {"reestrNumber": row["reestr_number"]},
                    save_as=f"card_{row['reestr_number']}.html",
                )
                card = parse_contract_card(card_html)
                if card["reestr_number"] != row["reestr_number"]:
                    card["reestr_number"] = row["reestr_number"]
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
        raise SystemExit(0)


def _backfill(args: argparse.Namespace) -> None:
    client = EisClient(delay=args.delay, raw_dir=RAW_DIR if args.keep_raw else None)
    db = Database(DB_PATH)
    rows = db.suppliers_without_email(args.limit)
    if not rows:
        logging.info("поставщиков без почты не осталось")
        return
    logging.info("добираю контакты: %d поставщиков", len(rows))
    updated = 0
    for inn, reestr_number, price in rows:
        html = client.get(CARD_PATH, {"reestrNumber": reestr_number},
                          save_as=f"card_{reestr_number}.html")
        card = parse_contract_card(html)
        for supplier in card["suppliers"]:
            if supplier["inn"] == inn and supplier["email"]:
                db.update_supplier_contacts(supplier)
                updated += 1
                logging.info("  %s %s | %s", inn, supplier["email"], supplier["name"][:40])
                break
        else:
            logging.info("  %s: почты в карточке нет", inn)
        db.mark_contacts_checked(inn)
        db.commit()
    left = len(db.suppliers_without_email(100000))
    logging.info("обновлено %d, осталось без почты %d", updated, left)


def cmd_leads(args: argparse.Namespace) -> None:
    """Ранжированный список поставщиков по правилам приоритизации, в CSV."""
    import csv
    import sqlite3

    from .scoring import rank

    today = _parse_date(args.date) if args.date else date.today()
    conn = sqlite3.connect(DB_PATH)
    leads, dropped = rank(conn, today)
    if args.fresh_days is not None:
        leads = [l for l in leads if (today - l.signed_at).days <= args.fresh_days]
    if args.with_contact:
        leads = [l for l in leads if l.has_contact]
    out = ROOT / "data" / f"leads_{today.isoformat()}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["место", "балл", "поставщик", "ИНН", "цена контракта", "подписан",
                    "исполнение до", "контрактов в выборке", "телефон", "почта",
                    "реестровый номер", "сумма", "срок", "свежесть", "активность"])
        for i, l in enumerate(leads[: args.top], 1):
            w.writerow([i, l.score, l.name, l.inn, round(l.price), l.signed_at, l.execution_until,
                        l.contracts_in_sample, l.phone, l.email, l.reestr_number,
                        *(round(l.parts[k], 1) for k in ("size", "duration", "freshness", "activity"))])
    logging.info("кандидатов %d, отсеяно %s, в файл %d -> %s",
                 len(leads), dict(dropped), min(len(leads), args.top), out)


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
    b.add_argument("--limit", type=int, default=40, help="сколько поставщиков за запуск")
    b.add_argument("--delay", type=float, default=5.0)
    b.add_argument("--keep-raw", action="store_true")
    b.set_defaults(func=cmd_backfill)
    l = sub.add_parser("leads", help="ранжированный список поставщиков в CSV")
    l.add_argument("--top", type=int, default=100)
    l.add_argument("--fresh-days", type=int, help="только контракты, подписанные не раньше N дней назад")
    l.add_argument("--with-contact", action="store_true", help="только с телефоном или почтой")
    l.add_argument("--date", help="дата расчета, ДД.ММ.ГГГГ (по умолчанию сегодня)")
    l.set_defaults(func=cmd_leads)
    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
