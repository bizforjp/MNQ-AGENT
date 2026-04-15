"""
Finnhub adapter — §8.6 and §8.9 TC24/TC26/TC27.

The adapter's job is to fetch historical bars for the outage-recovery replay
path. The real implementation hits Finnhub's free-tier HTTP API (60 req/min
ceiling, well under the projected worst case of ~240 calls/month per OI-07).

For testing, tests inject a FakeFinnhub with the same shape (fetch_bars).
FinnhubError is raised on any unrecoverable failure (network, auth, bad data).
"""
from typing import List, Protocol
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError
import json

from backend.position_resolver import Bar


class FinnhubError(Exception):
    """Raised when Finnhub is unavailable or returns unusable data."""


class FinnhubAdapterProtocol(Protocol):
    def fetch_bars(
        self, *, symbol: str, start_ms: int, end_ms: int, interval: str = "15m"
    ) -> List[Bar]: ...


class FinnhubAdapter:
    """Real HTTP adapter. Kept minimal; tests always use FakeFinnhub."""

    BASE_URL = "https://finnhub.io/api/v1/stock/candle"

    def __init__(self, api_key: str = "", bar_interval_ms: int = 900_000,
                 http_open=urlopen, timeout_s: float = 10.0):
        if not api_key:
            raise FinnhubError("api_key required for real FinnhubAdapter")
        self.api_key = api_key
        self.bar_interval_ms = bar_interval_ms
        self._http_open = http_open
        self._timeout = timeout_s

    def fetch_bars(self, *, symbol, start_ms, end_ms, interval="15m"):
        params = {
            "symbol": symbol,
            "resolution": _interval_to_resolution(interval),
            "from": start_ms // 1000,
            "to": end_ms // 1000,
            "token": self.api_key,
        }
        url = f"{self.BASE_URL}?{urlencode(params)}"
        try:
            with self._http_open(url, timeout=self._timeout) as resp:
                payload = json.loads(resp.read())
        except (URLError, json.JSONDecodeError, TimeoutError) as e:
            raise FinnhubError(f"fetch failed: {e!r}") from e

        if payload.get("s") != "ok":
            raise FinnhubError(f"bad status: {payload.get('s')!r}")

        times = payload.get("t") or []
        opens = payload.get("o") or []
        highs = payload.get("h") or []
        lows = payload.get("l") or []
        closes = payload.get("c") or []
        if not (len(times) == len(opens) == len(highs) == len(lows) == len(closes)):
            raise FinnhubError("shape mismatch in Finnhub payload")

        return [
            Bar(
                bar_close_ms=int(t) * 1000 + self.bar_interval_ms,
                open=float(o), high=float(h), low=float(l), close=float(c),
            )
            for t, o, h, l, c in zip(times, opens, highs, lows, closes)
        ]


def _interval_to_resolution(interval: str) -> str:
    return {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(
        interval, "15"
    )
