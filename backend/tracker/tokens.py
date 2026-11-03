"""On-chain mint facts used for entry market caps.

Two batched getMultipleAccounts calls per 100 mints:
1. the mint accounts (jsonParsed): decimals, current supply, mint and freeze authority, Token-2022 extensions;
2. each mint's pump.fun bonding-curve PDA (seeds ["bonding-curve", mint] under the pump.fun program).
   An existing account owned by pump.fun proves the mint was created by pump.fun, even when the wallet
   only traded it after graduation (PumpSwap, Raydium). pump.fun mints are created with 1e9 tokens and
   no mint authority, which bounds their historical supply from above.
"""

from datetime import datetime, timedelta, timezone

from solders.pubkey import Pubkey
from sqlalchemy import select

from .db import TokenMint, now_iso
from .normalize import VENUES

PUMP_PROGRAM = next(p for p, name in VENUES.items() if name == "pump.fun")
MINT_TTL = timedelta(hours=24)
BATCH = 100


def pump_bonding_curve(mint: str) -> str:
    pda, _bump = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                             Pubkey.from_string(PUMP_PROGRAM))
    return str(pda)


def _parse_mint(value: dict | None) -> dict:
    if value is None:
        return {"exists": False}
    data = value.get("data")
    info = data.get("parsed", {}).get("info", {}) if isinstance(data, dict) else {}
    if not info or "supply" not in info:
        return {"exists": True, "program": value.get("owner")}  # not a parseable SPL mint
    return {"exists": True, "program": value.get("owner"), "decimals": info.get("decimals"),
            "supply_raw": str(info["supply"]), "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "extensions": [e.get("extension") for e in info.get("extensions") or [] if isinstance(e, dict)]}


def refresh_mints(session, rpc, mints: set[str], *, max_age: timedelta = MINT_TTL, max_batches: int = 10) -> dict:
    """Fetch facts for unknown or stale mints. Network errors propagate as ProviderError."""

    cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
    known = {m.mint: m for m in session.scalars(select(TokenMint).where(TokenMint.mint.in_(sorted(mints))))}
    # pump.fun origin never changes once proven; other facts (authority, supply) can.
    due = sorted(m for m in mints if m not in known or (known[m].fetched_at < cutoff and not known[m].pump_origin))
    report = {"mints": len(mints), "refreshed": 0, "pump_origin": 0, "rpc_calls": 0, "pending": 0}
    for start in range(0, len(due), BATCH):
        if start // BATCH >= max_batches:
            report["pending"] = len(due) - start
            break
        chunk = due[start:start + BATCH]
        curves = [pump_bonding_curve(m) for m in chunk]
        mint_values = rpc.accounts(chunk)
        curve_values = rpc.accounts(curves, encoding="base64", data_slice={"offset": 0, "length": 0})
        report["rpc_calls"] += 2
        for mint, curve, mint_value, curve_value in zip(chunk, curves, mint_values, curve_values):
            facts = _parse_mint(mint_value)
            row = known.get(mint) or TokenMint(mint=mint, source=rpc.source)
            for key in ("program", "decimals", "supply_raw", "mint_authority", "freeze_authority", "extensions"):
                setattr(row, key, facts.get(key))
            row.exists = facts["exists"]
            row.pump_bonding_curve = curve
            row.pump_origin = bool(curve_value) and curve_value.get("owner") == PUMP_PROGRAM
            row.source, row.fetched_at = rpc.source, now_iso()
            session.add(row)
            report["refreshed"] += 1
            report["pump_origin"] += int(row.pump_origin)
        session.commit()
    return report
