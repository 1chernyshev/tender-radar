"""HTTP client for zakupki.gov.ru (ЕИС) public HTML pages.

The site serves plain HTTPS with a Russian root CA that is not in the macOS
trust store, so certificate verification is disabled on purpose. Requests are
throttled and retried to stay well within polite limits.
"""

from __future__ import annotations

import gzip
import logging
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

BASE_URL = "https://zakupki.gov.ru"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


class BlockedError(RuntimeError):
    """ЕИС временно заблокировал IP за автоматический сбор.

    Повторять запросы в этом состоянии бессмысленно и вредно: сбор
    прекращается до следующего запуска.
    """


class EisClient:
    def __init__(self, delay: float = 2.0, retries: int = 3, raw_dir: Path | None = None):
        self.delay = delay
        self.retries = retries
        self.raw_dir = raw_dir
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        })
        self.session.verify = False

    def get(self, path: str, params: dict | None = None, save_as: str | None = None) -> str:
        url = path if path.startswith("http") else BASE_URL + path
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=60)
                if resp.status_code == 403 or "подозрительная активность" in resp.text:
                    raise BlockedError(
                        "ЕИС заблокировал IP за автоматический сбор; сбор остановлен"
                    )
                if resp.status_code == 200 and len(resp.text) > 1000:
                    if self.raw_dir and save_as:
                        self.raw_dir.mkdir(parents=True, exist_ok=True)
                        # Сжимаем: страница ЕИС весит ~170 КБ, в gzip ~20 КБ.
                        with gzip.open(self.raw_dir / (save_as + ".gz"), "wt", encoding="utf-8") as fh:
                            fh.write(resp.text)
                    return resp.text
                log.warning("GET %s -> %s (%d bytes), attempt %d", resp.url, resp.status_code, len(resp.text), attempt)
            except BlockedError:
                raise
            except requests.RequestException as exc:
                log.warning("GET %s failed: %s, attempt %d", url, exc, attempt)
            time.sleep(self.delay * attempt * 2)
        raise RuntimeError(f"failed to fetch {url} after {self.retries} attempts")

    def _throttle(self) -> None:
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
