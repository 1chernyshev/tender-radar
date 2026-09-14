"""HTML parsers for the ЕИС contract registry (44-ФЗ)."""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

_WS = re.compile(r"\s+")


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip()


def _money(value: str) -> float | None:
    """'1 000 000,00 ₽' -> 1000000.0"""
    m = re.search(r"[\d\s ]+(?:,\d+)?", value)
    if not m:
        return None
    num = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _date(value: str) -> date | None:
    m = re.search(r"\d{2}\.\d{2}\.\d{4}", value or "")
    if not m:
        return None
    return datetime.strptime(m.group(0), "%d.%m.%Y").date()


def parse_contract_search(html: str) -> list[dict]:
    """Parse a contract-registry search results page into summary rows."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for card in soup.select("div.search-registry-entry-block"):
        number_link = card.select_one(".registry-entry__header-mid__number a")
        if not number_link:
            continue
        reestr_number = re.sub(r"\D", "", _text(number_link))
        row = {
            "reestr_number": reestr_number,
            "status": _text(card.select_one(".registry-entry__header-mid__title")),
            "customer_name": "",
            "contract_number": "",
            "object": _text(card.select_one(".lots-wrap-content__body__val")),
            "price": _money(_text(card.select_one(".price-block__value"))),
        }
        for block in card.select(".registry-entry__body-block"):
            title = _text(block.select_one(".registry-entry__body-title"))
            value = _text(block.select_one(".registry-entry__body-href, .registry-entry__body-value"))
            if title == "Заказчик":
                row["customer_name"] = value
            elif title == "Контракт":
                row["contract_number"] = value.lstrip("№ ").strip()
        titles = [_text(t) for t in card.select(".data-block__title")]
        values = [_text(v) for v in card.select(".data-block__value")]
        dates = dict(zip(titles, values))
        row["signed_at"] = _date(dates.get("Заключение контракта", ""))
        row["execution_until"] = _date(dates.get("Срок исполнения", ""))
        row["published_at"] = _date(dates.get("Размещен контракт в реестре контрактов", ""))
        row["updated_at"] = _date(dates.get("Обновлен контракт в реестре контрактов", ""))
        rows.append(row)
    return rows


def parse_contract_card(html: str) -> dict:
    """Parse a contract card (common-info page) into a flat dict."""
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    for section in soup.select("section.section, section.blockInfo__section"):
        title = section.select_one("span.section__title")
        info = section.select_one("span.section__info")
        if title is not None and info is not None:
            fields.setdefault(_text(title), _text(info))

    def pick(label_start: str) -> str:
        for k, v in fields.items():
            if k.startswith(label_start):
                return v
        return ""

    card = {
        "reestr_number": re.sub(r"\D", "", pick("Реестровый номер контракта")),
        "status": pick("Статус контракта"),
        "ikz": pick("Идентификационный код закупки"),
        "procurement_method": pick("Способ определения поставщика"),
        "single_supplier_basis": pick("Основание заключения контракта с единственным поставщиком"),
        "bank_support": pick("Информация о банковском и (или) казначейском сопровождении"),
        "customer_name": pick("Полное наименование заказчика"),
        "customer_inn": pick("ИНН"),
        "budget_level": pick("Уровень бюджета"),
        "signed_at": _date(pick("Дата заключения контракта")),
        "contract_number": pick("Номер контракта"),
        "subject": pick("Предмет контракта"),
        "price": _money(pick("Цена контракта") or pick("Максимальное значение цены контракта")),
        "price_is_max": not pick("Цена контракта") and bool(pick("Максимальное значение цены контракта")),
        "security_amount": _money(pick("Размер обеспечения исполнения контракта")),
        "execution_from": _date(pick("Дата начала исполнения контракта")),
        "execution_until": _date(pick("Дата окончания исполнения контракта")),
        "advance_raw": pick("Размер аванса"),
        "delivery_place": pick("Место поставки товара"),
    }
    # ЕИС omits the "Размер аванса" field entirely when the contract has no advance,
    # so a missing field on an otherwise parsed card means 0 %.
    card["advance_pct"] = _advance_pct(card["advance_raw"]) if card["advance_raw"] else (0.0 if fields else None)
    card["suppliers"] = _parse_suppliers(soup)
    return card


def _advance_pct(raw: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", raw or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_suppliers(soup: BeautifulSoup) -> list[dict]:
    suppliers = []
    table = soup.select_one("div.participantsInnerHtml table")
    if table is None:
        return suppliers
    for tr in table.select("tbody tr"):
        cols = tr.select("td")
        if len(cols) < 4:
            continue
        first = cols[0]
        name = _text(first)
        inn = kpp = ""
        for section in first.select("section"):
            spans = section.select("span")
            if len(spans) >= 2:
                label, value = _text(spans[0]), _text(spans[1])
                if label.startswith("ИНН"):
                    inn = value
                elif label.startswith("КПП"):
                    kpp = value
        # strip the "ИНН: ... КПП: ..." tail that _text picked up from nested sections
        name = re.split(r"\s+ИНН:", name)[0].strip()
        contact = _text(cols[3])
        email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", contact)
        phone = contact.replace(email.group(0), "").strip() if email else contact
        suppliers.append({
            "name": name,
            "inn": inn,
            "kpp": kpp,
            "country": _text(cols[1]),
            "address": _text(cols[2]),
            "phone": phone,
            "email": email.group(0) if email else "",
        })
    return suppliers
