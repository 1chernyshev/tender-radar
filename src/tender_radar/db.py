"""SQLite storage. Kept deliberately simple for the first iteration;
the schema mirrors what the ЕИС pages expose and is the source for analytics."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    reestr_number      TEXT PRIMARY KEY,
    status             TEXT,
    ikz                TEXT,
    procurement_method TEXT,
    single_supplier_basis TEXT,
    bank_support       TEXT,
    customer_name      TEXT,
    customer_inn       TEXT,
    budget_level       TEXT,
    funding_source     TEXT,
    contract_number    TEXT,
    subject            TEXT,
    object             TEXT,
    price              REAL,
    price_is_max       INTEGER,
    security_amount    REAL,
    advance_pct        REAL,
    advance_raw        TEXT,
    signed_at          DATE,
    execution_from     DATE,
    execution_until    DATE,
    published_at       DATE,
    updated_at         DATE,
    delivery_place     TEXT,
    card_fetched_at    TIMESTAMP,
    first_seen_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS suppliers (
    inn      TEXT PRIMARY KEY,
    kpp      TEXT,
    name     TEXT,
    country  TEXT,
    address  TEXT,
    phone    TEXT,
    email    TEXT,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contract_suppliers (
    reestr_number TEXT REFERENCES contracts(reestr_number),
    inn           TEXT REFERENCES suppliers(inn),
    PRIMARY KEY (reestr_number, inn)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pages      INTEGER,
    found      INTEGER,
    new_cards  INTEGER,
    note       TEXT
);
"""


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def upsert_summary(self, row: dict) -> bool:
        """Insert a search-result row; returns True if the contract is new."""
        cur = self.conn.execute(
            "SELECT 1 FROM contracts WHERE reestr_number = ?", (row["reestr_number"],)
        )
        is_new = cur.fetchone() is None
        self.conn.execute(
            """INSERT INTO contracts (reestr_number, status, customer_name, contract_number,
                   object, price, signed_at, execution_until, published_at, updated_at)
               VALUES (:reestr_number, :status, :customer_name, :contract_number,
                   :object, :price, :signed_at, :execution_until, :published_at, :updated_at)
               ON CONFLICT(reestr_number) DO UPDATE SET
                   status = excluded.status, updated_at = excluded.updated_at""",
            {k: _iso(v) for k, v in row.items()},
        )
        return is_new

    def needs_card(self, reestr_number: str) -> bool:
        cur = self.conn.execute(
            "SELECT card_fetched_at FROM contracts WHERE reestr_number = ?", (reestr_number,)
        )
        r = cur.fetchone()
        return r is None or r["card_fetched_at"] is None

    def save_card(self, card: dict) -> None:
        payload = {k: _iso(v) for k, v in card.items() if k != "suppliers"}
        self.conn.execute(
            """UPDATE contracts SET
                   status = :status, ikz = :ikz, procurement_method = :procurement_method,
                   single_supplier_basis = :single_supplier_basis, bank_support = :bank_support,
                   customer_name = :customer_name, customer_inn = :customer_inn,
                   budget_level = :budget_level, funding_source = :funding_source, contract_number = :contract_number,
                   subject = :subject, price = :price, price_is_max = :price_is_max,
                   security_amount = :security_amount, advance_pct = :advance_pct,
                   advance_raw = :advance_raw, signed_at = :signed_at,
                   execution_from = :execution_from, execution_until = :execution_until,
                   delivery_place = :delivery_place, card_fetched_at = CURRENT_TIMESTAMP
               WHERE reestr_number = :reestr_number""",
            payload,
        )
        for s in card["suppliers"]:
            if not s["inn"]:
                continue
            self.conn.execute(
                """INSERT INTO suppliers (inn, kpp, name, country, address, phone, email)
                   VALUES (:inn, :kpp, :name, :country, :address, :phone, :email)
                   ON CONFLICT(inn) DO UPDATE SET name = excluded.name,
                       address = excluded.address, phone = excluded.phone, email = excluded.email""",
                s,
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO contract_suppliers (reestr_number, inn) VALUES (?, ?)",
                (card["reestr_number"], s["inn"]),
            )

    def suppliers_without_email(self, limit: int) -> list[tuple[str, str, float]]:
        """Поставщики без почты, начиная с самых крупных контрактов."""
        cur = self.conn.execute(
            """SELECT cs.inn, c.reestr_number, c.price
                 FROM contract_suppliers cs
                 JOIN contracts c ON c.reestr_number = cs.reestr_number
                 JOIN suppliers s ON s.inn = cs.inn
                WHERE s.email = ''
                  AND c.price IS NOT NULL
                GROUP BY cs.inn
                HAVING c.price = MAX(c.price)
                ORDER BY c.price DESC
                LIMIT ?""",
            (limit,),
        )
        return [(r["inn"], r["reestr_number"], r["price"]) for r in cur.fetchall()]

    def update_supplier_contacts(self, supplier: dict) -> None:
        self.conn.execute(
            """UPDATE suppliers SET phone = :phone, email = :email,
                   address = :address, country = :country
                 WHERE inn = :inn""",
            supplier,
        )

    def log_fetch(self, pages: int, found: int, new_cards: int, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (pages, found, new_cards, note) VALUES (?, ?, ?, ?)",
            (pages, found, new_cards, note),
        )

    def commit(self) -> None:
        self.conn.commit()

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "contracts": q("SELECT COUNT(*) FROM contracts"),
            "with_card": q("SELECT COUNT(*) FROM contracts WHERE card_fetched_at IS NOT NULL"),
            "suppliers": q("SELECT COUNT(*) FROM suppliers"),
            "total_price": q("SELECT COALESCE(SUM(price), 0) FROM contracts"),
        }
