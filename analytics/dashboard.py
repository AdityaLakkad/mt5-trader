"""
analytics/dashboard.py
=======================
Real-time terminal dashboard using Rich Live.
Runs in a background thread.
"""

import threading
import time
from typing import Optional

from orders.order_manager import OrderManager, OrderSide
from analytics.performance import PerformanceAnalytics

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.layout import Layout
    from rich import box
    _RICH = True
except ImportError:
    _RICH = False

_SPARKS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list, width: int = 40) -> str:
    if len(values) < 2:
        return "─" * width
    tail = values[-width:]
    lo, hi = min(tail), max(tail)
    span = hi - lo or 1.0
    return "".join(_SPARKS[int((v - lo) / span * (len(_SPARKS) - 1))] for v in tail)


class Dashboard:

    def __init__(self, order_manager: OrderManager,
                 analytics: PerformanceAnalytics,
                 refresh_rate: float = 2.0):
        self.om           = order_manager
        self.analytics    = analytics
        self.refresh_rate = refresh_rate
        self._running     = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not _RICH:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        console = Console()
        try:
            with Live(self._build(), console=console,
                      refresh_per_second=1 / self.refresh_rate,
                      screen=False) as live:
                while self._running:
                    live.update(self._build())
                    time.sleep(self.refresh_rate)
        except Exception:
            pass

    def _build(self):
        from datetime import datetime
        balance   = self.om.balance
        equity    = self.om.equity
        initial   = self.om.initial_balance
        open_pnl  = equity - balance
        total_pnl = balance - initial
        dd_pct    = max(0.0, (initial - equity) / initial * 100) if initial else 0.0

        def coloured(val: float) -> Text:
            return Text(f"${val:+.2f}", style="green" if val >= 0 else "red")

        # Header
        grid = Table.grid(expand=True, padding=(0, 3))
        for _ in range(7):
            grid.add_column(justify="right")
        grid.add_row(*[Text(h, style="bold white") for h in
                       ["BALANCE","EQUITY","OPEN P&L","TOTAL P&L","DRAWDOWN","OPEN","CLOSED"]])
        dd_color = "red" if dd_pct > 10 else "yellow" if dd_pct > 5 else "green"
        grid.add_row(
            Text(f"${balance:,.2f}", style="bold cyan"),
            Text(f"${equity:,.2f}",  style="bold cyan"),
            coloured(open_pnl),
            coloured(total_pnl),
            Text(f"{dd_pct:.2f}%", style=dd_color),
            Text(str(self.om.open_trade_count), style="yellow"),
            Text(str(len(self.om.closed_trades)), style="white"),
        )
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        header = Panel(grid,
                       title=f"[bold magenta]🚀 MT5 Paper Trader[/]  [dim]{now}[/]",
                       border_style="magenta")

        # Positions
        pt = Table(title="📂 Open Positions", box=box.SIMPLE_HEAVY,
                   header_style="bold yellow", expand=True)
        for col in ["ID","Symbol","Side","Lots","Entry","SL","TP"]:
            pt.add_column(col)
        for pos in self.om.open_positions.values():
            side_txt = Text(pos.side.value,
                            style="green" if pos.side == OrderSide.BUY else "red")
            pt.add_row(
                pos.position_id[:6], pos.symbol, side_txt,
                f"{pos.volume:.2f}", f"{pos.entry_price:.5f}",
                f"{pos.sl:.5f}" if pos.sl else "—",
                f"{pos.tp:.5f}" if pos.tp else "—",
            )
        if not self.om.open_positions:
            pt.add_row(*["—"] * 7)

        # Trades
        tt = Table(title="📋 Recent Trades (last 10)", box=box.SIMPLE_HEAVY,
                   header_style="bold cyan", expand=True)
        for col in ["Symbol","Side","Entry","Exit","P&L","Reason","Dur"]:
            tt.add_column(col)
        for tr in list(reversed(self.om.closed_trades[-10:])):
            side_txt = Text(tr.side.value,
                            style="green" if tr.side == OrderSide.BUY else "red")
            pnl_txt  = Text(f"${tr.net_pnl:+.2f}",
                            style="green" if tr.net_pnl >= 0 else "red")
            tt.add_row(
                tr.symbol, side_txt,
                f"{tr.entry_price:.5f}", f"{tr.exit_price:.5f}",
                pnl_txt, tr.close_reason,
                f"{tr.duration_seconds/60:.0f}m",
            )
        if not self.om.closed_trades:
            tt.add_row(*["—"] * 7)

        # Sparkline
        vals  = [e["equity"] for e in self.analytics.equity_curve]
        spark = _sparkline(vals)
        start = vals[0]  if vals else initial
        end   = vals[-1] if vals else initial
        color = "green" if end >= start else "red"
        spark_panel = Panel(
            Text(f"  {spark}  ${start:,.0f} → ${end:,.0f}", style=color),
            title="[bold]Equity[/]", border_style="dim"
        )

        layout = Layout()
        layout.split_column(
            Layout(header,     name="h", size=6),
            Layout(name="mid"),
            Layout(spark_panel,name="s", size=4),
        )
        layout["mid"].split_row(Layout(pt), Layout(tt))
        return layout
