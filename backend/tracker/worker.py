"""Background worker: `python -m tracker.worker` (same environment variables as the API).

Run exactly one worker per database. TRACKER_REFRESH_MINUTES (default 30, 0 disables) sets how often
watchlist wallets are re-synced. A stream thread (tracker.stream, WebSocket; TRACKER_STREAM=0 disables) alerts on
watchlist trades a second or two after the block; a watcher thread polls the watchlist every TRACKER_WATCH_SECONDS
(default 20, 0 disables) as its fallback. All three share one rate-limited RPC client. Stop with Ctrl+C or SIGTERM; the job in progress stops after its
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
from .stream import Stream
from .stream import enabled as stream_enabled
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
    if stream_enabled():
        stream = Stream(sessions, rpc, prices)
        threading.Thread(target=stream.run_forever, args=(stop,), daemon=True, name="tracker-stream").start()
    Worker(sessions, rpc, prices, quotes=quotes, history=GeckoTerminal()).run_forever(stop)


if __name__ == "__main__":
    main()
