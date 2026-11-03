"""Current token quotes from DEX Screener's public API (no key). Used only for mark-to-market of open
positions: a current price never stands in for a historical entry price or market cap.

GET /tokens/v1/solana/{comma-separated mints} (up to 30) returns pairs; per mint the pair with the most USD
liquidity where the mint is the base token is used.
"""

import json
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx

from .solana import ProviderError

BATCH = 30


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


class DexScreenerQuotes:
    source = "https://api.dexscreener.com"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=True,
                                             headers={"User-Agent": "fomo-smart-money-research"})
        self.lock = threading.Lock()
        self.last_call = 0.0

    def _get(self, url: str):
        for attempt in range(3):
            with self.lock:
                time.sleep(max(0, 0.25 - (time.monotonic() - self.last_call)))  # well under 300/min
                try:
                    r = self.client.get(url)
                except httpx.HTTPError:
                    raise ProviderError("Fuente de cotizaciones inaccesible; revisa la red") from None
                finally:
                    self.last_call = time.monotonic()
            if r.status_code == 429 and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if r.status_code != 200:
                raise ProviderError(f"La fuente de cotizaciones devolvió HTTP {r.status_code}", 503)
            try:
                return json.loads(r.text)
            except ValueError:
                raise ProviderError("Respuesta de cotizaciones inválida") from None
        raise ProviderError("Fuente de cotizaciones limitada temporalmente", 503)

    def quotes(self, mints: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for start in range(0, len(mints), BATCH):
            chunk = mints[start:start + BATCH]
            pairs = self._get(f"{self.source}/tokens/v1/solana/{','.join(chunk)}")
            if not isinstance(pairs, list):
                raise ProviderError("Respuesta de cotizaciones inválida")
            for pair in pairs:
                base = (pair.get("baseToken") or {}).get("address")
                price = _decimal(pair.get("priceUsd"))
                if base not in chunk or price is None:
                    continue
                liquidity = _decimal((pair.get("liquidity") or {}).get("usd")) or Decimal(0)
                if base in out and out[base]["liquidity_usd"] >= liquidity:
                    continue
                out[base] = {"price_usd": price, "liquidity_usd": liquidity,
                             "market_cap_usd": _decimal(pair.get("marketCap")), "fdv_usd": _decimal(pair.get("fdv")),
                             "pair_address": pair.get("pairAddress"), "dex_id": pair.get("dexId"),
                             "symbol": (pair.get("baseToken") or {}).get("symbol")}
        return out
