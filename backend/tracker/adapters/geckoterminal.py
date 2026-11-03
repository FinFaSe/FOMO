"""Pool OHLCV candles from GeckoTerminal's public API (no key; 30 calls/min; free history covers ~6 months).

Third-party market data, stored and labeled as such (source `geckoterminal`), never as on-chain evidence.
- GET /networks/solana/tokens/{mint}/pools: pools of a token; the deepest one where the mint is the base token.
- GET /networks/solana/pools/{pool}/ohlcv/{minute|hour|day}?aggregate=1&before_timestamp=…&limit=…&currency=token
  returns [timestamp, open, high, low, close, volume] rows, newest first; `currency=token` prices the base token in
  the pool's quote token (SOL for SOL pairs; confirmed on 2026-11-03 against DEX Screener, ratio 0.996). Intervals
  without trades are absent.
- GET /networks/solana/pools/{pool}/trades: the pool's last 300 trades of the past day, parsed by GeckoTerminal
  (transaction, signer, side, amounts). Used to cross-check our normalizer (tracker.crosscheck).
- GET /networks/solana/trending_pools and /networks/solana/dexes/{dex}/pools: pool samples per DEX.
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

    def trending_pools(self, page: int = 1) -> list[dict]:
        body = self._get(f"/networks/{self.network}/trending_pools", {"page": page, "include": "dex"})
        out = []
        for pool in (body or {}).get("data") or []:
            attributes, relations = pool.get("attributes") or {}, pool.get("relationships") or {}
            dex = ((relations.get("dex") or {}).get("data") or {}).get("id")
            if attributes.get("address") and dex:
                out.append({"pool": attributes["address"], "dex": dex, "name": attributes.get("name")})
        return out

    def dex_pools(self, dex: str, page: int = 1) -> list[str]:
        body = self._get(f"/networks/{self.network}/dexes/{dex}/pools", {"page": page})
        return [p["attributes"]["address"] for p in (body or {}).get("data") or []
                if isinstance((p.get("attributes") or {}).get("address"), str)]

    def pool_trades(self, pool: str) -> list[dict]:
        """GeckoTerminal's parsed trades of a pool (last day, at most 300); malformed or anomalous rows dropped."""
        body = self._get(f"/networks/{self.network}/pools/{pool}/trades")
        out = []
        for row in (body or {}).get("data") or []:
            a = row.get("attributes") or {}
            amounts = [_decimal(a.get("from_token_amount")), _decimal(a.get("to_token_amount"))]
            texts = [a.get(k) for k in ("tx_hash", "tx_from_address", "from_token_address", "to_token_address")]
            if a.get("is_anomalous") or a.get("kind") not in ("buy", "sell") or None in amounts \
                    or not all(isinstance(t, str) and t for t in texts) or min(amounts) <= 0:
                continue
            out.append({"tx": a["tx_hash"], "signer": a["tx_from_address"], "kind": a["kind"],
                        "from_token": a["from_token_address"], "to_token": a["to_token_address"],
                        "from_amount": amounts[0], "to_amount": amounts[1],
                        "volume_usd": _decimal(a.get("volume_in_usd")), "block_time": a.get("block_timestamp")})
        return out
