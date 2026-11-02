import hashlib
import json
import os
import threading
import time
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
    """Documented JSON-RPC only. Bounded retries, finalized reads, no credentials in logs."""

    chain = "solana"

    def __init__(self, endpoint: str | None = None, client: httpx.Client | None = None):
        self.endpoint = endpoint or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("SOLANA_RPC_URL requiere HTTPS sin credenciales en userinfo")
        self.source = "https://" + parsed.hostname  # paths and query may contain provider credentials
        self.client = client or httpx.Client(timeout=25, follow_redirects=False, trust_env=True)
        self.lock = threading.Lock()
        self.last_call = 0.0

    def call(self, method: str, params: list):
        for attempt in range(3):
            with self.lock:
                time.sleep(max(0, 0.3 - (time.monotonic() - self.last_call)))
                try:
                    r = self.client.post(self.endpoint, json={"jsonrpc": "2.0", "id": 1,
                                                             "method": method, "params": params})
                except httpx.HTTPError:
                    raise ProviderError("RPC inaccesible; revisa red y configuración del proveedor") from None
                finally:
                    self.last_call = time.monotonic()
            if r.status_code in (429, 502, 503, 504):
                if attempt == 2:
                    raise ProviderError("RPC limitado o temporalmente no disponible; ingesta parcial", 503)
                try:
                    delay = min(5, max(1, float(r.headers.get("retry-after", "1"))))
                except ValueError:
                    delay = 1
                time.sleep(delay * (attempt + 1))
                continue
            if r.status_code != 200:
                raise ProviderError(f"El RPC devolvió HTTP {r.status_code}")
            try:
                body = r.json()
                if body.get("error"):
                    code = body["error"].get("code")
                    if code in (429, -32005) and attempt < 2:
                        time.sleep(attempt + 1)
                        continue
                    raise ProviderError(f"Error JSON-RPC {code}; consulta cobertura o límites")
                return body["result"]
            except (ValueError, KeyError, TypeError):
                raise ProviderError("Respuesta RPC inválida") from None

    def signatures(self, wallet: str, *, before: str | None = None, limit: int = 10):
        opts = {"commitment": "finalized", "limit": limit}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [wallet, opts])

    def transaction(self, signature: str):
        result = self.call("getTransaction", [signature, {"commitment": "finalized", "encoding": "jsonParsed",
                                                         "maxSupportedTransactionVersion": 1}])
        if result is None:
            raise ProviderError("Transacción no disponible en finalized; no se cuenta como verificada", 409)
        if signature not in result.get("transaction", {}).get("signatures", []):
            raise ProviderError("La firma devuelta por RPC no corresponde a la solicitada")
        return result


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
