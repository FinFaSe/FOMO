import hashlib
import heapq
import itertools
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlsplit

import httpx


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 502):
        self.message, self.status = message, status
        super().__init__(message)


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SolanaRPC:
    """Documented JSON-RPC only. Bounded retries, finalized reads, no credentials in logs.

    Request starts are spaced by SOLANA_RPC_MIN_INTERVAL seconds (default 0.3, under the public endpoint's
    per-method limit) across threads, but a slow response no longer blocks the next request: historical
    getTransaction calls take ~3 s on the public RPC, so `transactions()` keeps up to SOLANA_RPC_CONCURRENCY
    (default 6) in flight. Raise both with a paid provider. The spacing adapts: a rate-limit answer doubles it
    for every thread at once (and honors Retry-After), each success relaxes it back toward the minimum.
    """

    attempts = 5
    max_interval = 5.0

    chain = "solana"

    def __init__(self, endpoint: str | None = None, client: httpx.Client | None = None, *,
                 min_interval: float | None = None, concurrency: int | None = None):
        self.endpoint = endpoint or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("SOLANA_RPC_URL requiere HTTPS sin credenciales en userinfo")
        self.source = "https://" + parsed.hostname  # paths and query may contain provider credentials
        self.client = client or httpx.Client(timeout=25, follow_redirects=False, trust_env=True)
        self.min_interval = (min_interval if min_interval is not None
                             else float(os.getenv("SOLANA_RPC_MIN_INTERVAL", "0.3")))
        self.concurrency = max(1, concurrency if concurrency is not None
                               else int(os.getenv("SOLANA_RPC_CONCURRENCY", "6")))
        self.lock = threading.Condition()
        self.interval = self.min_interval  # current spacing, between min_interval and max_interval
        self.next_slot = 0.0
        self.waiting: list[tuple[int, int]] = []  # (priority, arrival) of requests waiting for a slot
        self.tickets = itertools.count()
        self._full_history: tuple[float, bool] | None = None

    def _wait_turn(self, urgent: bool = False):
        """Wait for the next request slot (spaced by `interval`). Urgent requests (the transaction behind a
        provisional alert) are served before routine ones already waiting (syncs, watcher, finalization), so an
        alert never queues behind bulk work. The request itself runs outside the lock."""
        with self.lock:
            ticket = (0 if urgent else 1, next(self.tickets))
            heapq.heappush(self.waiting, ticket)
            self.lock.notify_all()  # the head of the queue may have changed
            while True:
                now = time.monotonic()
                head = self.waiting[0] == ticket
                if head and now >= self.next_slot:
                    heapq.heappop(self.waiting)
                    self.next_slot = now + self.interval
                    self.lock.notify_all()
                    return
                self.lock.wait(self.next_slot - now if head else None)

    def _slow_down(self, retry_after: str | None):
        """Rate limited: double the spacing for every thread and pause them all (Retry-After if given)."""
        try:
            pause = min(10.0, max(0.0, float(retry_after))) if retry_after else 0.0
        except ValueError:
            pause = 0.0
        with self.lock:
            self.interval = min(self.max_interval, max(self.interval * 2, max(self.min_interval, 0.05) * 2))
            self.next_slot = max(self.next_slot, time.monotonic() + max(pause, self.interval))

    def _speed_up(self):
        with self.lock:
            self.interval = max(self.min_interval, self.interval * 0.95)

    def call(self, method: str, params: list, *, urgent: bool = False):
        for attempt in range(self.attempts):
            self._wait_turn(urgent)
            try:
                r = self.client.post(self.endpoint, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            except httpx.HTTPError:
                raise ProviderError("RPC inaccesible; revisa red y configuración del proveedor") from None
            limited = r.status_code in (429, 502, 503, 504)
            body = None
            if r.status_code == 200:
                try:
                    body = r.json()
                    code = body["error"].get("code") if body.get("error") else None
                except (ValueError, AttributeError, TypeError):
                    raise ProviderError("Respuesta RPC inválida") from None
                limited = code in (429, -32005)
            if limited:
                self._slow_down(r.headers.get("retry-after"))
                if attempt == self.attempts - 1:
                    raise ProviderError("RPC limitado o temporalmente no disponible; ingesta parcial", 503)
                continue
            if r.status_code != 200:
                raise ProviderError(f"El RPC devolvió HTTP {r.status_code}")
            self._speed_up()
            if body.get("error"):
                raise ProviderError(f"Error JSON-RPC {body['error'].get('code')}; consulta cobertura o límites")
            try:
                return body["result"]
            except (KeyError, TypeError):
                raise ProviderError("Respuesta RPC inválida") from None

    def full_history(self) -> bool:
        """True when the provider serves the whole ledger (its first available block is the genesis slot).

        Non-archival providers answer signature listings and transactions only for recent days, so a listing
        that ends there is the provider's horizon, not the wallet's first transaction. Cached for 10 minutes.
        """
        now = time.monotonic()
        if self._full_history is None or now - self._full_history[0] > 600:
            self._full_history = (now, self.call("getFirstAvailableBlock", []) == 0)
        return self._full_history[1]

    def signatures(self, wallet: str, *, before: str | None = None, until: str | None = None, limit: int = 10,
                   commitment: str = "finalized"):
        opts = {"commitment": commitment, "limit": limit}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return self.call("getSignaturesForAddress", [wallet, opts])

    def accounts(self, addresses: list[str], *, encoding: str = "jsonParsed", data_slice: dict | None = None):
        """getMultipleAccounts (max 100). Returns one value per address: the account dict or None."""
        if len(addresses) > 100:
            raise ValueError("getMultipleAccounts admite como máximo 100 direcciones")
        opts = {"commitment": "finalized", "encoding": encoding}
        if data_slice is not None:
            opts["dataSlice"] = data_slice
        result = self.call("getMultipleAccounts", [addresses, opts])
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != len(addresses):
            raise ProviderError("Respuesta getMultipleAccounts inválida")
        return values

    def transaction(self, signature: str, *, commitment: str = "finalized", urgent: bool = False):
        result = self.call("getTransaction", [signature, {"commitment": commitment, "encoding": "jsonParsed",
                                                         "maxSupportedTransactionVersion": 1}], urgent=urgent)
        if result is None:
            raise ProviderError(f"Transacción no disponible en {commitment}; no se cuenta como verificada", 409)
        if signature not in result.get("transaction", {}).get("signatures", []):
            raise ProviderError("La firma devuelta por RPC no corresponde a la solicitada")
        return result

    def confirmed_transaction(self, signature: str):
        """`confirmed` (supermajority voted, ~1 s after the block) instead of `finalized` (~13 s): for provisional
        alerts only; stored evidence stays finalized."""
        return self.transaction(signature, commitment="confirmed", urgent=True)

    def signature_statuses(self, signatures: list[str]) -> list[dict | None]:
        """getSignatureStatuses with history search: None for a signature the cluster does not know."""
        if len(signatures) > 256:
            raise ValueError("getSignatureStatuses admite como máximo 256 firmas")
        result = self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}])
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != len(signatures):
            raise ProviderError("Respuesta getSignatureStatuses inválida")
        return values

    def transactions(self, signatures: list[str]) -> dict[str, dict | ProviderError]:
        """Several transactions fetched concurrently; each value is the payload or the ProviderError it raised."""

        def one(signature):
            try:
                return signature, self.transaction(signature)
            except ProviderError as exc:
                return signature, exc

        if not signatures:
            return {}
        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(signatures))) as pool:
            return dict(pool.map(one, signatures))


def observe(transaction: dict, wallet: str) -> dict:
    """Balance changes are evidence of movement, NOT proof of a swap or a USD valuation."""

    meta = transaction.get("meta") or {}
    keys = transaction["transaction"]["message"]["accountKeys"]
    addresses = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]
    deltas: dict[str, Decimal] = {}
    for field, direction in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for balance in meta.get(field, []):
            if balance.get("owner") == wallet:
                q = balance["uiTokenAmount"]
                amount = Decimal(q["amount"]) / Decimal(10) ** q["decimals"]
                deltas[balance["mint"]] = deltas.get(balance["mint"], Decimal(0)) + direction * amount
    index = addresses.index(wallet) if wallet in addresses else None
    native = None
    if index is not None and meta.get("postBalances") and meta.get("preBalances"):
        native = str(Decimal(meta["postBalances"][index] - meta["preBalances"][index]) / Decimal(10) ** 9)
    return {"slot": transaction["slot"], "block_time": transaction.get("blockTime"),
            "status": "unavailable" if transaction.get("meta") is None else
                      ("failed" if meta.get("err") else "finalized"),
            "wallet_is_signer": wallet in signers, "signers": signers,
            "token_deltas": {k: str(v) for k, v in deltas.items() if v},
            "native_sol_delta": native, "network_fee_lamports": meta.get("fee"),
            "classification": "unclassified_movement", "usd_valuation": "unknown"}
