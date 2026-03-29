"""
analytics/performance.py
=========================
Performance metrics, equity curve, and reporting.
"""

import logging
import math
from datetime import datetime
from typing import List, Optional

import pandas as pd

from orders.order_manager import ClosedTrade, OrderManager

logger = logging.getLogger(__name__)


class PerformanceAnalytics:

    def __init__(self, order_manager: OrderManager):
        self.om = order_manager
        self.equity_curve: List[dict] = []

    def update(self) -> None:
        self.equity_curve.append({
            "time":    datetime.utcnow(),
            "equity":  round(self.om.equity,  4),
            "balance": round(self.om.balance, 4),
        })

    def compute(self) -> dict:
        trades = self.om.closed_trades
        if not trades:
            return {"error": "No closed trades yet."}

        net_pnls     = [t.net_pnl for t in trades]
        winners      = [p for p in net_pnls if p > 0]
        losers       = [p for p in net_pnls if p <= 0]
        total_net    = sum(net_pnls)
        gross_profit = sum(winners) if winners else 0.0
        gross_loss   = abs(sum(losers)) if losers else 0.0
        win_rate     = len(winners) / len(trades) * 100
        profit_factor= gross_profit / gross_loss if gross_loss > 0 else math.inf
        avg_win      = sum(winners) / len(winners) if winners else 0.0
        avg_loss     = sum(losers)  / len(losers)  if losers  else 0.0
        expectancy   = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

        eq_series    = pd.Series([e["equity"] for e in self.equity_curve])
        max_dd, max_dd_pct = self._max_drawdown(eq_series)
        sharpe       = self._sharpe_ratio(eq_series)
        durations    = [t.duration_seconds for t in trades]
        avg_dur_min  = sum(durations) / len(durations) / 60 if durations else 0.0

        return {
            "total_trades":            len(trades),
            "winners":                 len(winners),
            "losers":                  len(losers),
            "win_rate_pct":            round(win_rate, 2),
            "total_net_pnl":           round(total_net, 2),
            "gross_profit":            round(gross_profit, 2),
            "gross_loss":              round(gross_loss, 2),
            "profit_factor":           round(profit_factor, 3),
            "avg_win":                 round(avg_win, 2),
            "avg_loss":                round(avg_loss, 2),
            "expectancy":              round(expectancy, 2),
            "max_drawdown":            round(max_dd, 2),
            "max_drawdown_pct":        round(max_dd_pct, 2),
            "sharpe_ratio":            round(sharpe, 3),
            "initial_balance":         self.om.initial_balance,
            "final_balance":           round(self.om.balance, 2),
            "final_equity":            round(self.om.equity, 2),
            "return_pct":              round(total_net / self.om.initial_balance * 100, 2),
            "avg_trade_duration_min":  round(avg_dur_min, 1),
        }

    def per_symbol_breakdown(self) -> pd.DataFrame:
        trades = self.om.closed_trades
        if not trades:
            return pd.DataFrame()
        rows = []
        for sym in sorted({t.symbol for t in trades}):
            sym_trades = [t for t in trades if t.symbol == sym]
            pnls = [t.net_pnl for t in sym_trades]
            wins = [p for p in pnls if p > 0]
            rows.append({
                "symbol":     sym,
                "trades":     len(sym_trades),
                "win_rate_%": round(len(wins) / len(sym_trades) * 100, 1),
                "net_pnl":    round(sum(pnls), 2),
                "avg_pnl":    round(sum(pnls) / len(pnls), 2),
            })
        return pd.DataFrame(rows).set_index("symbol")

    def trades_dataframe(self) -> pd.DataFrame:
        trades = self.om.closed_trades
        if not trades:
            return pd.DataFrame()
        return pd.DataFrame([{
            "id":           t.position_id,
            "strategy":     t.strategy_id,
            "symbol":       t.symbol,
            "side":         t.side.value,
            "volume":       t.volume,
            "entry":        t.entry_price,
            "exit":         t.exit_price,
            "sl":           t.sl,
            "tp":           t.tp,
            "open_time":    t.open_time,
            "close_time":   t.close_time,
            "duration_min": round(t.duration_seconds / 60, 1),
            "pnl":          round(t.pnl, 2),
            "commission":   round(t.commission, 4),
            "net_pnl":      round(t.net_pnl, 2),
            "close_reason": t.close_reason,
        } for t in trades])

    def print_report(self) -> None:
        metrics = self.compute()
        if "error" in metrics:
            print(f"\n[Analytics] {metrics['error']}\n")
            return
        try:
            from rich.console import Console
            from rich.table import Table
            from rich import box
            console = Console()
            console.rule("[bold cyan]📊 Paper Trading Report")
            t = Table(box=box.ROUNDED, header_style="bold magenta")
            t.add_column("Metric",  style="cyan",  min_width=28)
            t.add_column("Value",   justify="right", min_width=14)
            rows = [
                ("Total Trades",         f"{metrics['total_trades']}"),
                ("Winners / Losers",     f"{metrics['winners']} / {metrics['losers']}"),
                ("Win Rate",             f"{metrics['win_rate_pct']:.2f}%"),
                ("Profit Factor",        f"{metrics['profit_factor']:.3f}"),
                ("Expectancy",           f"${metrics['expectancy']:.2f}"),
                ("Total Net PnL",        f"${metrics['total_net_pnl']:+.2f}"),
                ("Gross Profit",         f"${metrics['gross_profit']:.2f}"),
                ("Gross Loss",           f"${metrics['gross_loss']:.2f}"),
                ("Max Drawdown",         f"${metrics['max_drawdown']:.2f}"),
                ("Max Drawdown %",       f"{metrics['max_drawdown_pct']:.2f}%"),
                ("Sharpe Ratio",         f"{metrics['sharpe_ratio']:.3f}"),
                ("Return %",             f"{metrics['return_pct']:+.2f}%"),
                ("Initial Balance",      f"${metrics['initial_balance']:.2f}"),
                ("Final Balance",        f"${metrics['final_balance']:.2f}"),
                ("Avg Trade Duration",   f"{metrics['avg_trade_duration_min']:.1f} min"),
            ]
            for label, value in rows:
                t.add_row(label, value)
            console.print(t)
        except ImportError:
            print("\n===== Paper Trading Report =====")
            for k, v in metrics.items():
                print(f"  {k:30s}: {v}")

    def plot_equity_curve(self, save_path: Optional[str] = None) -> None:
        if not self.equity_curve:
            return
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            logger.error("matplotlib not installed.")
            return
        df = pd.DataFrame(self.equity_curve)
        fig, ax = plt.subplots(figsize=(14, 5))
        ax.plot(df["time"], df["equity"],  label="Equity",  color="#2196F3", linewidth=1.5)
        ax.plot(df["time"], df["balance"], label="Balance", color="#4CAF50", linewidth=1.0, linestyle="--")
        ax.axhline(y=self.om.initial_balance, color="grey", linestyle=":", linewidth=0.8)
        ax.set_title("Paper Trading Equity Curve")
        ax.set_xlabel("Time (UTC)")
        ax.set_ylabel("Account Value ($)")
        ax.legend()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        fig.autofmt_xdate()
        plt.tight_layout()
        if save_path:
            plt.savefig(save_path, dpi=150)
            logger.info(f"[Analytics] Equity curve saved to {save_path}")
        else:
            plt.show()

    @staticmethod
    def _max_drawdown(equity: pd.Series):
        if equity.empty:
            return 0.0, 0.0
        roll_max = equity.cummax()
        dd       = roll_max - equity
        dd_pct   = dd / roll_max * 100
        return float(dd.max()), float(dd_pct.max())

    @staticmethod
    def _sharpe_ratio(equity: pd.Series, risk_free: float = 0.0) -> float:
        if len(equity) < 2:
            return 0.0
        returns = equity.pct_change().dropna()
        if returns.std() == 0:
            return 0.0
        ann_factor = math.sqrt(252 * 1440)
        return float((returns.mean() - risk_free) / returns.std() * ann_factor)
