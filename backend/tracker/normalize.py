"""Swap normalization from stored finalized transactions (jsonParsed). No network access.

Trades are derived from the wallet's net balance changes, not from decoding instruction data,
so any route through a recognized venue normalizes the same way:

- SOL leg = change in the wallet's own lamports plus the lamports of every token account it owns
  (pre/post token balances with `owner == wallet`), plus the network fee when the wallet paid it.
  Rent moved into the wallet's own new token accounts, or refunded by closing them, nets to zero;
  wrapped SOL (wSOL) is counted through its account's lamports, never twice.
- Token legs = changes of non-quote mints owned by the wallet.

A trade is `verified` only when a recognized venue program ran, the wallet signed, and exactly one
token was exchanged for exactly one quote asset in the opposite direction. The same pattern without
a recognized venue stays `inferred`. Movements without an opposite quote leg are transfers.

Jito tips paid by the wallet are split from the SOL leg into `tip_sol` (a cost, like the network fee).
Known contamination of the SOL leg: tips to other services and rent paid to accounts the wallet does not
own (e.g. protocol PDAs) are counted as part of the trade, which slightly raises costs and lowers
proceeds. Venue fees are already inside the swapped amount, i.e. prices are effective prices.
"""

from decimal import Decimal

NORMALIZER_VERSION = "balance-delta-v3"  # v2: token decimals per event; v3: Jito tips split from the swap
# Jito's eight tip accounts, as the block engine's getTipAccounts returns them (re-checked by tracker.smoke). A tip
# is a cost of getting the trade landed, not part of the swapped amount: it is reported next to the network fee so
# the effective price is not inflated by it.
JITO_TIP_ACCOUNTS = frozenset({
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
})
LAMPORTS = Decimal(10) ** 9
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "Es9vMFrzaCERmJfrF4H2FYD4KLNRFeaDs3YdGBWbhTRc": "USDT"}
# Mainnet program IDs from each project's documentation/SDK. A router counts as a venue because the
# balance-delta method does not depend on which pools it touched.
VENUES = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium-amm-v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium-cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium-clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "raydium-launchlab",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "jupiter-v6",
}


def _amount(balance: dict) -> Decimal:
    q = balance["uiTokenAmount"]
    return Decimal(q["amount"]) / Decimal(10) ** q["decimals"]


def _programs(transaction: dict) -> list[str]:
    meta = transaction.get("meta") or {}
    found = []
    instructions = list(transaction["transaction"]["message"].get("instructions", []))
    for inner in meta.get("innerInstructions") or []:
        instructions.extend(inner.get("instructions", []))
    for ix in instructions:
        program = ix.get("programId")
        if program and program not in found:
            found.append(program)
    return found


def legs(transaction: dict, wallet: str) -> dict:
    """Net balance changes of the wallet, separated into SOL, stable and token legs."""

    meta = transaction.get("meta") or {}
    keys = transaction["transaction"]["message"]["accountKeys"]
    addresses = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    signers = [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    tokens: dict[str, Decimal] = {}
    decimals: dict[str, int] = {}
    owned_accounts: set[int] = set()
    for field, direction in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for balance in meta.get(field) or []:
            if balance.get("owner") != wallet:
                continue
            if balance.get("accountIndex") is not None:
                owned_accounts.add(balance["accountIndex"])
            tokens[balance["mint"]] = tokens.get(balance["mint"], Decimal(0)) + direction * _amount(balance)
            decimals[balance["mint"]] = balance["uiTokenAmount"]["decimals"]
    lamports = 0
    indexes = ({addresses.index(wallet)} if wallet in addresses else set()) | owned_accounts
    for i in indexes:
        if i < len(pre) and i < len(post):
            lamports += post[i] - pre[i]
    fee_payer = bool(addresses) and addresses[0] == wallet
    fee = meta.get("fee") or 0
    tip = 0
    if fee_payer:  # a tip is paid by the transaction's payer; a relayer's tip is not the wallet's cost
        tip = sum(post[i] - pre[i] for i, a in enumerate(addresses)
                  if a in JITO_TIP_ACCOUNTS and i < len(pre) and i < len(post) and post[i] > pre[i])
    trade_lamports = lamports + (fee + tip if fee_payer else 0)
    tokens.pop(WSOL, None)  # counted through the wSOL account lamports above
    stables = {STABLES[m]: tokens.pop(m) for m in list(tokens) if m in STABLES and tokens[m]}
    return {"sol": Decimal(trade_lamports) / LAMPORTS,
            "network_fee_sol": Decimal(fee if fee_payer else 0) / LAMPORTS, "tip_sol": Decimal(tip) / LAMPORTS,
            "stables": stables, "tokens": {m: d for m, d in tokens.items() if d}, "decimals": decimals,
            "fee_payer": fee_payer, "wallet_is_signer": wallet in signers}


def normalize(transaction: dict, wallet: str) -> list[dict]:
    """Return trade/transfer events for the wallet. Failed or unavailable transactions yield none."""

    meta = transaction.get("meta")
    if meta is None or meta.get("err"):
        return []
    leg = legs(transaction, wallet)
    programs = _programs(transaction)
    venues = [VENUES[p] for p in programs if p in VENUES]
    tokens = leg["tokens"]
    if not tokens:
        return []
    quotes = [("SOL", leg["sol"])] if leg["sol"] else []
    quotes += sorted(leg["stables"].items())
    events = []
    single = len(tokens) == 1
    for index, (mint, delta) in enumerate(sorted(tokens.items())):
        opposite = [(asset, amount) for asset, amount in quotes if (amount < 0) != (delta < 0)]
        issues = []
        quote_asset = quote_amount = None
        if single and len(opposite) == 1:
            quote_asset, quote_amount = opposite[0][0], abs(opposite[0][1])
            kind = "buy" if delta > 0 else "sell"
        elif single and not opposite:
            kind = "transfer_in" if delta > 0 else "transfer_out"
        else:
            # Token-for-token or several quote legs: inventory is known, the price is not allocatable.
            kind = "buy" if delta > 0 else "sell"
            issues.append("multi_leg_swap_quote_not_allocatable")
        is_trade = kind in ("buy", "sell")
        if not is_trade:
            trade = "not_a_trade"
        elif venues and leg["wallet_is_signer"] and quote_amount is not None:
            trade = "verified"
        else:
            trade = "inferred"
            if not venues:
                issues.append("no_recognized_venue")
            if not leg["wallet_is_signer"]:
                issues.append("wallet_not_signer")
        quantity = abs(delta)
        events.append({
            "event_index": index, "mint": mint, "kind": kind, "token_amount": quantity,
            "decimals": leg["decimals"][mint],
            "quote_asset": quote_asset, "quote_amount": quote_amount,
            "price": quote_amount / quantity if quote_amount is not None else None,
            # The network fee is charged once per transaction; attach it to the first event only.
            "network_fee_sol": leg["network_fee_sol"] if index == 0 else Decimal(0),
            "tip_sol": leg["tip_sol"] if index == 0 else Decimal(0),
            "venues": venues, "trade": trade, "issues": issues,
        })
    return events
