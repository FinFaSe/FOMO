from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_bytes(value: str) -> bytes:
    number = 0
    for c in value:
        if c not in BASE58:
            raise ValueError("Se requiere base58 válido")
        number = number * 58 + BASE58.index(c)
    return b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")


def validate_address(value: str) -> str:
    if len(base58_bytes(value)) != 32:
        raise ValueError("La dirección Solana debe codificar 32 bytes")
    return value


def validate_signature(value: str) -> str:
    if len(base58_bytes(value)) != 64:
        raise ValueError("La firma Solana debe codificar 64 bytes")
    return value


address = validate_address
signature = validate_signature


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("El timestamp debe incluir zona horaria")
    return value.astimezone(timezone.utc)


Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=40, decimal_places=18)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filters(StrictModel):
    max_entry_market_cap_usd: Money = Decimal("250000")
    min_realized_roi_pct: Money = Decimal("500")
    min_win_rate: float = Field(default=0.60, ge=0, le=1)
    min_evaluable_positions: int = Field(default=30, ge=1, le=100000)
    analysis_days: int = Field(default=90, ge=1, le=3650)
    conservative_win_rate: bool = True


class WalletCreate(StrictModel):
    address: str = Field(min_length=32, max_length=44)
    label: str = Field(default="", max_length=80)

    _address = field_validator("address")(validate_address)


class WalletUpdate(StrictModel):
    favorite: bool


class LedgerEvent(StrictModel):
    """An external assertion, never a verified fact merely because it was imported."""

    signature: str = Field(max_length=88)
    event_index: int = Field(default=0, ge=0, le=10000)
    token: str = Field(max_length=44)
    timestamp: datetime
    kind: Literal["buy", "sell", "transfer_in", "transfer_out"]
    quantity: Decimal = Field(gt=0, allow_inf_nan=False, max_digits=40, decimal_places=18)
    gross_usd: Money | None = None
    fees_usd: Money | None = None
    market_cap_usd: Money | None = None
    fdv_usd: Money | None = None
    liquidity_usd: Money | None = None
    source_url: str = Field(max_length=500)

    _token = field_validator("token")(validate_address)
    _signature = field_validator("signature")(validate_signature)
    _timestamp = field_validator("timestamp")(utc)

    @field_validator("source_url")
    @classmethod
    def public_reference(cls, value: str) -> str:
        from urllib.parse import urlsplit

        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Usa una referencia HTTPS pública sin credenciales, query ni fragmento")
        return value


class LedgerImport(StrictModel):
    events: list[LedgerEvent] = Field(min_length=1, max_length=2000)


class SyncRequest(StrictModel):
    days: int = Field(default=90, ge=1, le=3650)
    max_pages: int = Field(default=2, ge=1, le=20)
    page_size: int = Field(default=10, ge=1, le=100)
    # Re-download stored transactions from the newest one and compare digests; ignores cursors.
    revalidate: bool = False


class JobRequest(SyncRequest):
    """Background sync: larger page budget per round, follow-up rounds until the window is complete."""

    max_pages: int = Field(default=5, ge=1, le=20)
    page_size: int = Field(default=50, ge=1, le=100)
    until_complete: bool = True
    max_rounds: int = Field(default=30, ge=1, le=200)

    def sync_request(self) -> SyncRequest:
        return SyncRequest(**self.model_dump(include=set(SyncRequest.model_fields)))


class SeedCreate(StrictModel):
    mint: str = Field(max_length=44)
    label: str = Field(default="", max_length=80)
    # Optional pool address for tokens that are not from pump.fun (their bonding curve is derived).
    pool: str | None = Field(default=None, max_length=44)
    early_trades: int = Field(default=100, ge=10, le=300)
    max_pages: int = Field(default=20, ge=1, le=100)
    # Optional declared peak market cap (e.g. from a chart); always labeled as declared, never verified.
    peak_market_cap_usd: Money | None = None
    peak_source_url: str | None = Field(default=None, max_length=500)

    _mint = field_validator("mint")(validate_address)

    @field_validator("pool")
    @classmethod
    def _pool(cls, value):
        return validate_address(value) if value is not None else None

    @field_validator("peak_source_url")
    @classmethod
    def _source(cls, value):
        if value is None:
            return value
        from urllib.parse import urlsplit

        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Usa una referencia HTTPS pública sin credenciales, query ni fragmento")
        return value


class DiscoveryRequest(StrictModel):
    token: str = Field(max_length=44)
    limit: int = Field(default=3, ge=1, le=10)

    _token = field_validator("token")(address)


class FomoResolve(StrictModel):
    handle: str = Field(pattern=r"^@?[a-zA-Z0-9_.-]{1,64}$")


class AlertsRead(StrictModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False


def hook_signatures(payload, limit: int = 100) -> list[str]:
    """Signatures from a provider webhook body: [{"signature": …}], {"signatures": […]}, Helius-style transaction
    objects ({"transaction": {"signatures": […]}}) or plain strings. Invalid entries are ignored."""
    items = payload.get("signatures", [payload]) if isinstance(payload, dict) else payload
    found = []
    for item in items if isinstance(items, list) else []:
        candidate = item
        if isinstance(item, dict):
            candidate = item.get("signature") or ((item.get("transaction") or {}).get("signatures") or [None])[0]
        if not isinstance(candidate, str):
            continue
        try:
            validate_signature(candidate)
        except ValueError:
            continue
        if candidate not in found:
            found.append(candidate)
    return found[:limit]


class BacktestRequest(StrictModel):
    wallets: list[str] = Field(default_factory=list, max_length=50)  # empty: watchlist, else all tracked
    delays: list[int] = Field(default_factory=lambda: [0, 5, 30, 60, 300], min_length=1, max_length=10)
    size_sol: Decimal = Field(default=Decimal("0.5"), gt=0, le=1000, allow_inf_nan=False)
    slippage_bps: Decimal = Field(default=Decimal(100), ge=0, le=5000, allow_inf_nan=False)
    impact_bps_per_sol: Decimal = Field(default=Decimal(0), ge=0, le=10000, allow_inf_nan=False)
    tx_cost_sol: Decimal = Field(default=Decimal("0.0001"), ge=0, le=1, allow_inf_nan=False)
    max_staleness_seconds: int = Field(default=300, ge=1, le=86400)
    since_days: int = Field(default=90, ge=1, le=3650)
    use_candles: bool = True  # pool candles (pessimistic fills) where no on-chain price is close enough

    @field_validator("wallets")
    @classmethod
    def _wallets(cls, values):
        return [validate_address(v) for v in values]

    @field_validator("delays")
    @classmethod
    def _delays(cls, values):
        if any(v < 0 or v > 86400 for v in values):
            raise ValueError("Los retrasos deben estar entre 0 y 86400 segundos")
        return sorted(set(values))


Role = Literal["viewer", "analyst", "admin"]


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class TokenCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class UserCreate(StrictModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: Role = "viewer"


class UserUpdate(StrictModel):
    role: Role | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=1, max_length=256)
