"""Command-line entry point: `python -m tender_radar fetch|stats`."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .client import EisClient
from .db import Database
from .parsers import parse_contract_card, parse_contract_search

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
    client = EisClient(delay=args.delay, raw_dir=RAW_DIR if args.keep_raw else None)
    db = Database(DB_PATH)
    date_to = _parse_date(args.date_to) if args.date_to else date.today()
    date_from = _parse_date(args.date_from) if args.date_from else date_to - timedelta(days=args.days)
    found = new_cards = 0
    for page in range(1, args.pages + 1):
        rows = []
        # ЕИС иногда отдает страницу поиска без результатов, хотя контракты есть.
        # Пустая первая страница — повод повторить запрос, а не считать, что данных нет.
        attempts = 3 if page == 1 else 1
        for attempt in range(1, attempts + 1):
            html = client.get(SEARCH_PATH, search_params(page, date_from, date_to), save_as=f"search_p{page}.html")
            rows = parse_contract_search(html)
            if rows or attempt == attempts:
                break
            logging.warning("page %d: пусто, повтор %d/%d", page, attempt, attempts - 1)
            time.sleep(args.delay * 5 * attempt)
        logging.info("page %d: %d contracts", page, len(rows))
        if not rows:
            if page == 1:
                logging.error("поиск за %s..%s вернул пусто после %d попыток", date_from, date_to, attempts)
            break
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
    db.log_fetch(args.pages, found, new_cards, f"{date_from}..{date_to}")
    db.commit()
    logging.info("done: found %d, new cards %d; stats %s", found, new_cards, db.stats())


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
    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
