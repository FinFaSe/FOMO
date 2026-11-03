"""Funding origins and linked wallets (entities).

Several wallets controlled by one actor look like independent smart money, and an insider funded by a token's
creator looks like an early genius. Links:

- `funded_by`: an address's first SOL came from another known address.
- `same_funder`: two addresses got their first SOL from the same wallet. Funders that move too much (exchanges,
  routers, bots: 1,000 signatures within a day) fund unrelated people and are not used to link.
- `co_buy`: two wallets bought the same seed token within one slot of each other in at least two seeds.

Linked addresses form entities (connected components). Links are evidence of possible common control, not proof.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from .adapters.solana import ProviderError
from .db import DiscoverySeed, EarlyBuy, Wallet, WalletFunding, now_iso
from .service import store_transaction

PAGE = 1000
OLDEST_KEPT = 10
HIGH_VOLUME_SPAN = 86_400  # a funder whose latest 1,000 signatures fit in a day is not a personal wallet
CO_BUY_SLOTS = 1
CO_BUY_MIN_SEEDS = 2
STALE_AFTER = timedelta(minutes=10)


def funding_json(row: WalletFunding | None) -> dict | None:
    if row is None:
        return None
    return {"address": row.address, "state": row.state, "funder": row.funder, "funder_kind": row.funder_kind,
            "signature": row.signature, "lamports": row.lamports, "reason": row.reason,
            "block_time": (datetime.fromtimestamp(row.block_time, timezone.utc).isoformat()
                           if row.block_time else None),
            "requested_by": row.requested_by, "fetched_at": row.fetched_at}


def enqueue_funding(session, addresses, requested_by: str = "manual") -> int:
    """Queue lookups for addresses without one. Returns how many were queued."""
    wanted = sorted(set(addresses))
    known = set(session.scalars(select(WalletFunding.address).where(WalletFunding.address.in_(wanted))))
    new = [WalletFunding(address=a, requested_by=requested_by) for a in wanted if a not in known]
    session.add_all(new)
    session.commit()
    return len(new)


def claim_funding(session, worker_id: str) -> WalletFunding | None:
    for row in list(session.scalars(select(WalletFunding).where(WalletFunding.state == "queued")
                                    .order_by(WalletFunding.created_at).limit(5))):
        won = session.execute(update(WalletFunding).where(WalletFunding.address == row.address,
                                                          WalletFunding.state == "queued")
                              .values(state="scanning", locked_by=worker_id, heartbeat_at=now_iso())).rowcount == 1
        session.commit()
        if won:
            session.refresh(row)
            return row
    return None


def recover_stale_funding(session) -> int:
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat()
    stale = list(session.scalars(select(WalletFunding).where(WalletFunding.state == "scanning",
                                                             WalletFunding.heartbeat_at < cutoff)))
    for row in stale:
        row.state, row.locked_by = "queued", None
    session.commit()
    return len(stale)


def _finish(session, row, state, reason=None):
    row.state, row.reason, row.locked_by, row.fetched_at = state, reason, None, now_iso()
    session.commit()
    return row


def lookup_funding(session, rpc, row: WalletFunding, *, max_pages: int = 10) -> WalletFunding:
    """List the address back to its first signature and find the first SOL it received from someone else."""
    try:
        oldest, before, reached = [], None, False
        for _ in range(max_pages):
            page = rpc.signatures(row.address, before=before, limit=PAGE)
            if page:
                oldest = (oldest + page)[-OLDEST_KEPT:]
                before = page[-1]["signature"]
            row.heartbeat_at = now_iso()
            session.commit()
            if len(page) < PAGE:
                reached = True
                break
        if not reached:
            return _finish(session, row, "unknown", "history_too_long")
        for item in [i for i in reversed(oldest) if not i.get("err")]:
            raw = store_transaction(session, rpc, item["signature"])
            meta = raw.payload.get("meta") or {}
            keys = raw.payload["transaction"]["message"]["accountKeys"]
            addresses = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
            if row.address not in addresses or not addresses or addresses[0] == row.address:
                continue  # the address paid for this transaction: not an incoming transfer from someone else
            i = addresses.index(row.address)
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(pre) and i < len(post) and post[i] > pre[i]:
                row.funder, row.signature, row.block_time = addresses[0], raw.signature, raw.block_time
                row.lamports = str(post[i] - pre[i])
                break
        if row.funder is None:
            return _finish(session, row, "unknown", "no_incoming_sol_in_first_transactions")
        recent = rpc.signatures(row.funder, limit=PAGE)
        times = [i["blockTime"] for i in recent if i.get("blockTime")]
        busy = len(recent) >= PAGE and times and max(times) - min(times) < HIGH_VOLUME_SPAN
        row.funder_kind = "high_volume" if busy else "wallet"
        return _finish(session, row, "done")
    except ProviderError as exc:
        session.rollback()
        return _finish(session, row, "failed", exc.message)


def links(session) -> list[dict]:
    out = []
    funded = {r.address: r for r in session.scalars(select(WalletFunding).where(WalletFunding.state == "done"))}
    known = set(funded) | set(session.scalars(select(Wallet.address)))
    known |= {c for c in session.scalars(select(DiscoverySeed.creator)) if c}
    known |= set(session.scalars(select(EarlyBuy.wallet)))
    groups = defaultdict(list)
    for row in funded.values():
        if row.funder_kind != "wallet":
            continue
        if row.funder in known:
            out.append({"a": row.address, "b": row.funder, "reason": "funded_by", "evidence": row.signature})
        groups[row.funder].append(row.address)
    for funder, members in groups.items():
        members.sort()
        for other in members[1:]:
            out.append({"a": members[0], "b": other, "reason": "same_funder", "evidence": funder})
    together = defaultdict(set)
    by_mint = defaultdict(list)
    for buy in session.scalars(select(EarlyBuy)):
        by_mint[buy.mint].append(buy)
    for mint, buys in by_mint.items():
        buys.sort(key=lambda b: b.slot)
        for i, first in enumerate(buys):
            for second in buys[i + 1:]:
                if second.slot - first.slot > CO_BUY_SLOTS:
                    break
                if first.wallet != second.wallet:
                    together[tuple(sorted((first.wallet, second.wallet)))].add(mint)
    for (a, b), mints in sorted(together.items()):
        if len(mints) >= CO_BUY_MIN_SEEDS:
            out.append({"a": a, "b": b, "reason": "co_buy", "evidence": sorted(mints)})
    return out


def entities(session, *, min_size: int = 2) -> list[dict]:
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    found = links(session)
    for link in found:
        ra, rb = find(link["a"]), find(link["b"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    members = defaultdict(set)
    for node in list(parent):
        members[find(node)].add(node)
    result = []
    for root, group in members.items():
        if len(group) < min_size:
            continue
        result.append({"id": root, "size": len(group), "members": sorted(group),
                       "links": [lk for lk in found if lk["a"] in group]})
    return sorted(result, key=lambda e: (-e["size"], e["id"]))


def entity_index(session) -> dict[str, dict]:
    """address -> entity, for every address that belongs to an entity of two or more."""
    return {m: e for e in entities(session) for m in e["members"]}
