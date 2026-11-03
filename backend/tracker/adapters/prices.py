"""Historical SOL/USD from Coinbase Exchange public 1-minute candles. No API key, no credentials.

GET /products/SOL-USD/candles?granularity=60&start=…&end=… returns [time, low, high, open, close, volume]
buckets, newest first, at most 300 per request; minutes without ticks are simply absent.
Pyth Benchmarks' public TradingView shim was retired on 2026-08-26, so it is not used.
"""

import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .solana import ProviderError

MAX_CANDLES = 300


class CoinbaseCandles:
    source = "https://api.exchange.coinbase.com"
    symbol = "SOL-USD"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=True,
                                             headers={"User-Agent": "fomo-smart-money-research"})
        self.lock = threading.Lock()
        self.last_call = 0.0

    def minute_candles(self, start: int, end: int) -> list[dict]:
        """Candles with start <= minute < end. The window must fit in one request (300 minutes)."""

        if end <= start or (end - start) // 60 > MAX_CANDLES:
            raise ValueError("Ventana de velas inválida")
        params = {"granularity": 60,
                  "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                  "end": datetime.fromtimestamp(end - 60, timezone.utc).isoformat()}
        for attempt in range(3):
            with self.lock:
                time.sleep(max(0, 0.25 - (time.monotonic() - self.last_call)))
                try:
                    r = self.client.get(f"{self.source}/products/{self.symbol}/candles", params=params)
                except httpx.HTTPError:
                    raise ProviderError("Fuente de precios inaccesible; revisa la red") from None
                finally:
                    self.last_call = time.monotonic()
            if r.status_code == 429 and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if r.status_code != 200:
                raise ProviderError(f"La fuente de precios devolvió HTTP {r.status_code}", 503)
            try:
                rows = json.loads(r.text, parse_float=Decimal)
                candles = [{"minute": int(row[0]), "low": Decimal(row[1]), "high": Decimal(row[2]),
                            "open": Decimal(row[3]), "close": Decimal(row[4])} for row in rows]
            except (ValueError, TypeError, IndexError):
                raise ProviderError("Respuesta de precios inválida") from None
            # The API may return buckets just outside the window; keep only the requested range.
            return [c for c in candles if start <= c["minute"] < end and c["minute"] % 60 == 0]
        raise ProviderError("Fuente de precios limitada temporalmente", 503)
