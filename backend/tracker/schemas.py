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


class DiscoveryRequest(StrictModel):
    token: str = Field(max_length=44)
    limit: int = Field(default=3, ge=1, le=10)

    _token = field_validator("token")(address)


class FomoResolve(StrictModel):
    handle: str = Field(pattern=r"^@?[a-zA-Z0-9_.-]{1,64}$")
