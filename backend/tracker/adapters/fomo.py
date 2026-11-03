import os
import threading
import time

import httpx

from .solana import ProviderError


class FomoAPI:
    """Independent third-party provider, NOT an official fomo.family integration.

    No private FOMO endpoints, session cookies, trading accounts, or wallet exports.
    Source-reported identity is never promoted to independently verified identity.
    """

    base_url = "https://api.fomoapi.io"

    def __init__(self, client: httpx.Client | None = None):
        self.key = os.getenv("FOMO_API_KEY")
        self.client = client or httpx.Client(timeout=25, follow_redirects=False, trust_env=True)
        self.lock = threading.Lock()
        self.last_call = 0.0

    def resolve(self, handle: str) -> dict:
        if not self.key:
            raise ProviderError("Configura FOMO_API_KEY de forma segura; no la pegues en el chat", 503)
        with self.lock:
            time.sleep(max(0, 3.1 - (time.monotonic() - self.last_call)))
            try:
                r = self.client.get(f"{self.base_url}/v2/users/{handle.lstrip('@')}",
                                    headers={"Authorization": f"Bearer {self.key}"})
            except httpx.HTTPError:
                raise ProviderError("FOMO API no está disponible") from None
            finally:
                self.last_call = time.monotonic()
        # Do not retry billed calls blindly; never leak request headers or upstream payloads in errors.
        if r.status_code != 200:
            raise ProviderError(f"FOMO API HTTP {r.status_code}; revisa clave, créditos o límite", 502)
        try:
            data = r.json()
        except ValueError:
            raise ProviderError("Respuesta FOMO API inválida") from None
        if data.get("available") is False or not data.get("wallets", {}).get("solana"):
            raise ProviderError("El proveedor no dispone de una wallet Solana para ese usuario", 404)
        return data
