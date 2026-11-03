"""Pool OHLCV candles from GeckoTerminal's public API (no key; 30 calls/min; free history covers ~6 months).

Third-party market data, stored and labeled as such (source `geckoterminal`), never as on-chain evidence.
- GET /networks/solana/tokens/{mint}/pools: pools of a token; the deepest one where the mint is the base token.
- GET /networks/solana/pools/{pool}/ohlcv/{minute|hour|day}?aggregate=1&before_timestamp=…&limit=…&currency=token
  returns [timestamp, open, high, low, close, volume] rows, newest first; `currency=token` prices the base token in
  the pool's quote token (SOL for SOL pairs). Intervals without trades are absent.
"""

import json
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx

from .solana import ProviderError

WSOL = "So11111111111111111111111111111111111111112"
MAX_CANDLES = 1000
TIMEFRAMES = {"minute": 60, "hour": 3600, "day": 86400}


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _token(relationship: dict | None) -> str | None:
    ident = ((relationship or {}).get("data") or {}).get("id") or ""
    return ident.split("_", 1)[1] if "_" in ident else None


class GeckoTerminal:
    source = "https://api.geckoterminal.com"
    network = "solana"

    def __init__(self, client: httpx.Client | None = None, spacing: float = 2.1):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=True,
                                             headers={"Accept": "application/json",
                                                      "User-Agent": "fomo-smart-money-research"})
        self.spacing = spacing  # 30 calls per minute
        self.lock = threading.Lock()
        self.last_call = 0.0

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(3):
            with self.lock:
                time.sleep(max(0, self.spacing - (time.monotonic() - self.last_call)))
                try:
                    r = self.client.get(f"{self.source}/api/v2{path}", params=params)
                except httpx.HTTPError:
                    raise ProviderError("Historial de precios inaccesible; revisa la red") from None
                finally:
                    self.last_call = time.monotonic()
            if r.status_code == 429 and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise ProviderError(f"El historial de precios devolvió HTTP {r.status_code}", 503)
            try:
                return json.loads(r.text)
            except ValueError:
                raise ProviderError("Respuesta de historial de precios inválida") from None
        raise ProviderError("Historial de precios limitado temporalmente", 503)

    def best_pool(self, mint: str) -> dict | None:
        """Deepest pool where `mint` is the base token, preferring SOL-quoted pools."""
        body = self._get(f"/networks/{self.network}/tokens/{mint}/pools", {"page": 1})
        candidates = []
        for pool in (body or {}).get("data") or []:
            attributes, relations = pool.get("attributes") or {}, pool.get("relationships") or {}
            if _token(relations.get("base_token")) != mint or not attributes.get("address"):
                continue
            quote = _token(relations.get("quote_token"))
            candidates.append({"pool": attributes["address"], "quote_mint": quote,
                               "dex": ((relations.get("dex") or {}).get("data") or {}).get("id"),
                               "name": attributes.get("name"),
                               "reserve_usd": _decimal(attributes.get("reserve_in_usd")) or Decimal(0)})
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p["quote_mint"] == WSOL, p["reserve_usd"]))

    def candles(self, pool: str, timeframe: str, before: int, limit: int = MAX_CANDLES) -> list[dict]:
        if timeframe not in TIMEFRAMES:
            raise ValueError("Intervalo de velas inválido")
        body = self._get(f"/networks/{self.network}/pools/{pool}/ohlcv/{timeframe}",
                         {"aggregate": 1, "before_timestamp": before, "limit": min(limit, MAX_CANDLES),
                          "currency": "token", "token": "base"})
        rows = (((body or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        out = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 5:
                continue
            values = [_decimal(v) for v in row[1:5]]
            if None in values or not isinstance(row[0], (int, float)):
                continue
            ts = int(row[0])
            if ts % TIMEFRAMES[timeframe] or ts >= before:
                continue  # misaligned or outside the requested range
            out.append({"ts": ts, "open": values[0], "high": values[1], "low": values[2], "close": values[3],
                        "volume": _decimal(row[5]) if len(row) > 5 else None})
        return sorted(out, key=lambda c: c["ts"])
