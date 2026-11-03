"""Background worker: `python -m tracker.worker` (same environment variables as the API).

Run exactly one worker per database. TRACKER_REFRESH_MINUTES (default 30, 0 disables) sets how often
watchlist wallets are re-synced. Stop with Ctrl+C or SIGTERM; the job in progress stops after its
current transaction and resumes from the stored cursors next time.
"""

import logging
import signal
import threading

from .adapters.prices import CoinbaseCandles
from .adapters.solana import SolanaRPC
from .db import database
from .jobs import Worker


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _, sessions = database()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    Worker(sessions, SolanaRPC(), CoinbaseCandles()).run_forever(stop)


if __name__ == "__main__":
    main()
