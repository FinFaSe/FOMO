"""Background worker: `python -m tracker.worker` (same environment variables as the API).

Run exactly one worker per database. TRACKER_REFRESH_MINUTES (default 30, 0 disables) sets how often
watchlist wallets are re-synced. A watcher thread polls watchlist wallets every TRACKER_WATCH_SECONDS
(default 20, 0 disables) for near-real-time alerts; both share one rate-limited RPC client. Stop with Ctrl+C or SIGTERM; the job in progress stops after its
current transaction and resumes from the stored cursors next time.
"""

import logging
import signal
import threading

from .adapters.geckoterminal import GeckoTerminal
from .adapters.prices import CoinbaseCandles
from .adapters.quotes import DexScreenerQuotes
from .adapters.solana import SolanaRPC
from .db import database
from .jobs import Worker
from .watch import Watcher


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _, sessions = database()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    rpc, prices, quotes = SolanaRPC(), CoinbaseCandles(), DexScreenerQuotes()
    watcher = Watcher(sessions, rpc, prices, quotes=quotes)
    if watcher.interval > 0:
        threading.Thread(target=watcher.run_forever, args=(stop,), daemon=True, name="tracker-watcher").start()
    Worker(sessions, rpc, prices, quotes=quotes, history=GeckoTerminal()).run_forever(stop)


if __name__ == "__main__":
    main()
