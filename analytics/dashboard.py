"""
analytics/dashboard.py
=======================
Minimal status display — prints a single summary line every N seconds.
Does NOT use Rich Live so logs scroll normally above it.
"""

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Dashboard:
    """
    Prints a compact one-line portfolio summary every refresh_rate seconds.
    Logs scroll freely above it — nothing is blocked or overwritten.

    Parameters
    ----------
    order_manager   : OrderManager or LiveOrderManager
    analytics       : PerformanceAnalytics
    refresh_rate    : seconds between summary lines (default 30s)
    """

    def __init__(self, order_manager, analytics,
                 refresh_rate: float = 30.0):
        self.om           = order_manager
        self.analytics    = analytics
        self.refresh_rate = refresh_rate
        self._running     = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while self._running:
            try:
                self._print_status()
            except Exception:
                pass
            time.sleep(self.refresh_rate)

    def _print_status(self) -> None:
        balance      = self.om.balance
        equity       = self.om.equity
        initial      = self.om.initial_balance
        open_pnl     = equity - balance
        total_pnl    = balance - initial
        open_count   = self.om.open_trade_count
        closed_count = len(getattr(self.om, "closed_trades", []))
        dd_pct       = max(0.0, (initial - equity) / initial * 100) if initial else 0.0

        # Open positions detail
        open_positions = getattr(self.om, "open_positions", {})
        pos_str = ""
        if open_positions:
            parts = []
            for pos in open_positions.values():
                side  = pos.side.value if hasattr(pos.side, "value") else str(pos.side)
                parts.append(
                    f"{pos.symbol} {side} {pos.volume:.2f}lots "
                    f"@ {pos.entry_price:.5f} "
                    f"SL={pos.sl:.5f}" if pos.sl else
                    f"{pos.symbol} {side} {pos.volume:.2f}lots @ {pos.entry_price:.5f}"
                )
            pos_str = " | ".join(parts)
        else:
            pos_str = "no open positions"

        # Win rate (if any closed trades)
        win_str = ""
        closed_trades = getattr(self.om, "closed_trades", [])
        if closed_trades:
            wins    = sum(1 for t in closed_trades
                          if getattr(t, "net_pnl", getattr(t, "pnl", 0)) > 0)
            win_rate = wins / len(closed_trades) * 100
            win_str = f"WR={win_rate:.0f}%"

        logger.info(
            f"[Portfolio] "
            f"Balance=${balance:,.2f} "
            f"Equity=${equity:,.2f} "
            f"OpenPnL={open_pnl:+.2f} "
            f"TotalPnL={total_pnl:+.2f} "
            f"DD={dd_pct:.1f}% "
            f"Open={open_count} "
            f"Closed={closed_count} "
            + (f"{win_str} " if win_str else "")
            + f"| {pos_str}"
        )