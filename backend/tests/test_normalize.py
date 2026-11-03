"""Swap normalization on SYNTHETIC jsonParsed transactions shaped like real RPC responses."""

from decimal import Decimal

from tracker.normalize import STABLES, VENUES, WSOL, fee_sol, legs, normalize

from .conftest import TOKEN, WALLET, b58

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
SYSTEM = "11111111111111111111111111111111"
COMPUTE = "ComputeBudget111111111111111111111111111111"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
USDC = next(m for m, name in STABLES.items() if name == "USDC")
RENT = 2039280  # rent-exempt minimum of a token account
SOL = 10 ** 9
OTHER = b58(333)
TOKEN_B = b58(444)
POOL = b58(555)


def balance(index, mint, owner, amount, decimals=6):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def tx(accounts, pre, post, *, pre_tokens=(), post_tokens=(), programs=(), inner=(), fee=5000,
       err=None, signers=None, block_time=1_800_000_000, signature=None, slot=None):
    """Build a SYNTHETIC finalized transaction. accounts[0] is the fee payer."""
    signers = signers if signers is not None else {accounts[0]}
    return {
        # ~2 slots per second, so transactions at different times land in different blocks.
        "slot": slot if slot is not None else block_time * 2, "blockTime": block_time, "version": 0,
        "transaction": {"signatures": [signature or b58(1, 64)], "message": {
            "accountKeys": [{"pubkey": a, "signer": a in signers, "writable": True, "source": "transaction"}
                            for a in accounts],
            "instructions": [{"programId": p, "accounts": [], "data": ""} for p in programs]}},
        "meta": {"err": err, "fee": fee, "preBalances": list(pre), "postBalances": list(post),
                 "preTokenBalances": list(pre_tokens), "postTokenBalances": list(post_tokens),
                 "innerInstructions": [{"index": 0, "instructions": [{"programId": p, "accounts": [], "data": ""}
                                                                     for p in inner]}] if inner else []},
    }


def pump_buy(sol=1 * SOL, tokens=3_500_000_000_000, fee=105_000, *, mint=TOKEN, program=PUMP, **kwargs):
    """Buy on the bonding curve, creating the wallet's token account (rent paid to its own account)."""
    ata, curve, curve_ata, fee_recipient = b58(601), b58(602), b58(603), b58(604)
    return tx([WALLET, ata, curve, curve_ata, fee_recipient],
              [10 * SOL, 0, 30 * SOL, RENT, 5 * SOL],
              [10 * SOL - sol - RENT - fee, RENT, 30 * SOL + sol * 99 // 100, RENT, 5 * SOL + sol // 100],
              pre_tokens=[balance(3, mint, curve, 800_000_000_000_000)],
              post_tokens=[balance(1, mint, WALLET, tokens), balance(3, mint, curve, 800_000_000_000_000 - tokens)],
              programs=[COMPUTE, ATA_PROGRAM, program], fee=fee, **kwargs)


def pump_sell(sol=3 * SOL // 2, tokens=3_500_000_000_000, fee=105_000, *, mint=TOKEN, program=PUMP, **kwargs):
    """Sell everything on the curve and close the token account (rent refunded to the wallet)."""
    ata, curve, curve_ata = b58(601), b58(602), b58(603)
    return tx([WALLET, ata, curve, curve_ata],
              [9 * SOL, RENT, 31 * SOL, RENT],
              [9 * SOL + sol + RENT - fee, 0, 31 * SOL - sol, RENT],
              pre_tokens=[balance(1, mint, WALLET, tokens), balance(3, mint, curve, 700_000_000_000_000)],
              post_tokens=[balance(3, mint, curve, 700_000_000_000_000 + tokens)],
              programs=[COMPUTE, program], fee=fee, **kwargs)


def test_pump_buy_nets_own_rent_and_separates_the_network_fee():
    [event] = normalize(pump_buy(), WALLET)
    assert event["kind"] == "buy" and event["mint"] == TOKEN
    assert event["quote_asset"] == "SOL" and event["quote_amount"] == Decimal("1")
    assert event["token_amount"] == Decimal("3500000")
    assert event["network_fee_sol"] == Decimal("0.000105")
    assert event["price"] == Decimal(1) / Decimal(3_500_000)
    assert event["venues"] == ["pump.fun"] and event["trade"] == "verified" and not event["issues"]


def test_pump_sell_counts_closed_account_rent_as_the_wallets_own():
    [event] = normalize(pump_sell(), WALLET)
    assert event["kind"] == "sell" and event["quote_amount"] == Decimal("1.5")
    assert event["token_amount"] == Decimal("3500000") and event["trade"] == "verified"


def test_temporary_wsol_account_and_router_with_inner_raydium():
    ata, pool = b58(611), b58(612)
    # The wSOL account is created and closed inside the transaction: it never shows in token balances.
    t = tx([WALLET, ata, pool], [5 * SOL, RENT, 50 * SOL], [5 * SOL - SOL // 2 - 5000, RENT, 50 * SOL + SOL // 2],
           pre_tokens=[balance(1, TOKEN, WALLET, 0)], post_tokens=[balance(1, TOKEN, WALLET, 2_000_000)],
           programs=[JUPITER], inner=[RAYDIUM])
    [event] = normalize(t, WALLET)
    assert event["kind"] == "buy" and event["quote_amount"] == Decimal("0.5")
    assert event["venues"] == ["jupiter-v6", "raydium-amm-v4"]


def test_persistent_wsol_account_is_counted_once_through_its_lamports():
    wsol_ata, ata, pool = b58(621), b58(622), b58(623)
    t = tx([WALLET, wsol_ata, ata, pool],
           [SOL, 2 * SOL + RENT, RENT, 40 * SOL], [SOL - 5000, 3 * SOL // 2 + RENT, RENT, 40 * SOL + SOL // 2],
           pre_tokens=[balance(1, WSOL, WALLET, 2 * SOL, 9), balance(2, TOKEN, WALLET, 0)],
           post_tokens=[balance(1, WSOL, WALLET, 3 * SOL // 2, 9), balance(2, TOKEN, WALLET, 1_000_000)],
           programs=[RAYDIUM])
    assert WSOL not in legs(t, WALLET)["tokens"]
    [event] = normalize(t, WALLET)
    assert event["kind"] == "buy" and event["quote_asset"] == "SOL" and event["quote_amount"] == Decimal("0.5")


def test_usdc_quote():
    usdc_ata, ata = b58(631), b58(632)
    t = tx([WALLET, usdc_ata, ata], [SOL, RENT, RENT], [SOL - 5000, RENT, RENT],
           pre_tokens=[balance(1, USDC, WALLET, 500_000_000), balance(2, TOKEN, WALLET, 0)],
           post_tokens=[balance(1, USDC, WALLET, 400_000_000), balance(2, TOKEN, WALLET, 7_000_000)],
           programs=[CLMM])
    [event] = normalize(t, WALLET)
    assert event["kind"] == "buy" and event["quote_asset"] == "USDC" and event["quote_amount"] == Decimal("100")
    assert event["venues"] == ["raydium-clmm"] and event["trade"] == "verified"


def test_incoming_transfer_paid_by_someone_else_is_not_a_trade():
    ata = b58(641)
    t = tx([OTHER, WALLET, ata], [SOL, SOL, 0], [SOL - 5000 - RENT, SOL, RENT],
           post_tokens=[balance(2, TOKEN, WALLET, 1_000_000)], programs=[ATA_PROGRAM])
    [event] = normalize(t, WALLET)
    assert event["kind"] == "transfer_in" and event["trade"] == "not_a_trade"
    assert event["network_fee_sol"] == 0 and event["quote_amount"] is None


def test_outgoing_transfer_that_pays_recipient_rent_is_not_a_sale():
    ata, recipient_ata = b58(651), b58(652)
    t = tx([WALLET, ata, OTHER, recipient_ata], [SOL, RENT, SOL, 0], [SOL - 5000 - RENT, RENT, SOL, RENT],
           pre_tokens=[balance(1, TOKEN, WALLET, 5_000_000)],
           post_tokens=[balance(1, TOKEN, WALLET, 4_000_000), balance(3, TOKEN, OTHER, 1_000_000)],
           programs=[ATA_PROGRAM])
    [event] = normalize(t, WALLET)
    assert event["kind"] == "transfer_out" and event["token_amount"] == Decimal("1")


def test_buy_pattern_without_recognized_venue_is_only_inferred():
    ata = b58(661)
    t = tx([WALLET, ata, OTHER], [SOL, RENT, SOL], [SOL // 2 - 5000, RENT, SOL + SOL // 2],
           pre_tokens=[balance(1, TOKEN, WALLET, 0)], post_tokens=[balance(1, TOKEN, WALLET, 1_000_000)],
           programs=[SYSTEM])
    [event] = normalize(t, WALLET)
    assert event["kind"] == "buy" and event["trade"] == "inferred"
    assert event["issues"] == ["no_recognized_venue"]


def test_failed_and_unavailable_transactions_yield_no_trades():
    assert normalize(pump_buy(err={"InstructionError": [2, {"Custom": 6001}]}), WALLET) == []
    assert normalize({**pump_buy(), "meta": None}, WALLET) == []


def test_token_for_token_swap_keeps_inventory_without_inventing_a_price():
    a_ata, b_ata = b58(671), b58(672)
    t = tx([WALLET, a_ata, b_ata], [SOL, RENT, RENT], [SOL - 5000, RENT, RENT],
           pre_tokens=[balance(1, TOKEN, WALLET, 9_000_000), balance(2, TOKEN_B, WALLET, 0)],
           post_tokens=[balance(1, TOKEN, WALLET, 0), balance(2, TOKEN_B, WALLET, 3_000_000)],
           programs=[JUPITER])
    events = {e["mint"]: e for e in normalize(t, WALLET)}
    assert events[TOKEN]["kind"] == "sell" and events[TOKEN_B]["kind"] == "buy"
    assert all(e["quote_amount"] is None and e["trade"] == "inferred" for e in events.values())
    assert all("multi_leg_swap_quote_not_allocatable" in e["issues"] for e in events.values())


def test_relayed_swap_without_wallet_fee_keeps_full_amount_and_requires_signature():
    ata, pool = b58(681), b58(682)
    t = tx([OTHER, WALLET, ata, pool], [SOL, 2 * SOL, RENT, SOL], [SOL - 5000, SOL, RENT, 2 * SOL],
           pre_tokens=[balance(2, TOKEN, WALLET, 0)], post_tokens=[balance(2, TOKEN, WALLET, 1_000_000)],
           programs=[PUMP], signers={OTHER, WALLET})
    [event] = normalize(t, WALLET)
    assert event["quote_amount"] == Decimal("1") and event["network_fee_sol"] == 0
    assert event["trade"] == "verified"
    t["transaction"]["message"]["accountKeys"][1]["signer"] = False
    [unsigned] = normalize(t, WALLET)
    assert unsigned["trade"] == "inferred" and "wallet_not_signer" in unsigned["issues"]


def test_venue_registry_has_unique_valid_program_ids():
    from tracker.schemas import address

    assert len(set(VENUES.values())) == len(VENUES)
    for program in VENUES:
        address(program)


RELAYER = b58(777)
DLMM = next(p for p, name in VENUES.items() if name == "meteora-dlmm")


def relayed_usdc_sell(refund_lamports, **kwargs):
    """Shaped like real relayed sells (2026-11-03): a terminal's relayer pays the fee, the trader sells the token,
    gets USDC through a SOL pool and a small SOL refund from the relayer."""
    usdc_ata, token_ata = b58(781), b58(782)
    return tx([RELAYER, WALLET, usdc_ata, token_ata, POOL],
              [SOL, 0, RENT, RENT, 30 * SOL], [SOL - 15000 - refund_lamports, refund_lamports, RENT, RENT, 30 * SOL],
              pre_tokens=[balance(2, USDC, WALLET, 0), balance(3, TOKEN, WALLET, 5_000_000)],
              post_tokens=[balance(2, USDC, WALLET, 21_575_044), balance(3, TOKEN, WALLET, 0)],
              programs=[COMPUTE, DLMM], fee=15000, signers={RELAYER, WALLET}, **kwargs)


def test_small_sol_refund_next_to_a_stablecoin_quote_is_a_fee_not_a_second_quote():
    [event] = normalize(relayed_usdc_sell(1_488_440), WALLET)
    assert event["kind"] == "sell" and event["trade"] == "verified" and event["venues"] == ["meteora-dlmm"]
    assert event["quote_asset"] == "USDC" and event["quote_amount"] == Decimal("21.575044")
    assert event["other_sol"] == Decimal("-0.00148844")  # a refund: a negative cost
    assert event["issues"] == ["small_sol_leg_counted_as_fee"] and event["network_fee_sol"] == 0  # relayer paid
    assert fee_sol({k: str(v) for k, v in event.items() if k.endswith("_sol")}) == Decimal("-0.00148844")


def test_a_real_sol_amount_next_to_a_stablecoin_stays_unallocatable():
    [event] = normalize(relayed_usdc_sell(50_000_000), WALLET)  # 0.05 SOL is not rent
    assert event["trade"] == "inferred" and event["quote_amount"] is None and event["other_sol"] == 0
    assert "multi_leg_swap_quote_not_allocatable" in event["issues"]
