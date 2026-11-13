"""Обновление карточек и добор контактов с общим ограниченным бюджетом."""
import logging
import math
from datetime import date

from .parsers import parse_contract_card

log = logging.getLogger(__name__)
CARD_PATH = '/epz/contract/contractCard/common-info.html'


def validate_card(card, expected_number):
    """Не приписывать чужую/неполную страницу запрошенному контракту."""
    if card.get('reestr_number') != expected_number:
        raise RuntimeError('Реестровый номер карточки не совпал с запросом')
    price = card.get('price')
    suppliers = card.get('suppliers') or []
    if (not card.get('status') or not isinstance(price, (int, float))
            or not math.isfinite(price) or price < 0 or not suppliers
            or any(not str(s.get('inn') or '').isdigit()
                   or len(str(s.get('inn') or '')) not in (10, 12) for s in suppliers)):
        raise RuntimeError('Неполная карточка: прежние данные сохранены')


def maintain_cards(db, client, limit=20, refresh_limit=5, stale_days=7, today=None):
    if not 0 <= refresh_limit <= limit <= 20 or stale_days < 1:
        raise ValueError('Нужны 0 <= refresh_limit <= limit <= 20 и stale_days >= 1')
    today = today or date.today()
    cache = {}
    refreshed = contacts = 0

    def load(number):
        if number not in cache:
            html = client.get(CARD_PATH, {'reestrNumber': number}, save_as=f'card_{number}.html')
            card = parse_contract_card(html)
            validate_card(card, number)
            cache[number] = card
        return cache[number]

    for number in db.stale_cards(refresh_limit, stale_days, today):
        card = load(number)
        db.save_card(card)
        db.commit()
        refreshed += 1
        log.info('обновлена известная карточка %s', number)

    # Весь этап, включая обновление, выполняет не более limit загрузок карточек.
    for inn, number, _ in db.suppliers_without_email(limit):
        if len(cache) >= limit and number not in cache:
            break
        card = load(number)
        for supplier in card['suppliers']:
            if supplier['inn'] == inn and supplier['email']:
                db.update_supplier_contacts(supplier)
                contacts += 1
                break
        db.mark_contacts_checked(inn)
        db.commit()
    result = {'requests': len(cache), 'refreshed': refreshed, 'contacts_updated': contacts}
    log.info('обслуживание карточек: %s; общий лимит %d', result, limit)
    return result
