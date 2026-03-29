"""
analytics/trade_journal.py
===========================
Exports trades and metrics to CSV and plain text.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.performance import PerformanceAnalytics

logger = logging.getLogger(__name__)


class TradeJournal:

    def __init__(self, analytics: PerformanceAnalytics,
                 output_dir: str = "./results",
                 run_label: str = ""):
        self.analytics  = analytics
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._ts    = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        self._label = f"_{run_label}" if run_label else ""

    def export(self) -> dict:
        paths = {}

        trades_df = self.analytics.trades_dataframe()
        if not trades_df.empty:
            p = self.output_dir / f"trades{self._label}_{self._ts}.csv"
            trades_df.to_csv(p)
            paths["trades"] = str(p)

        metrics = self.analytics.compute()
        if "error" not in metrics:
            metrics["run_label"]   = self._label.strip("_")
            metrics["exported_at"] = self._ts
            p = self.output_dir / f"summary{self._label}_{self._ts}.csv"
            pd.DataFrame([metrics]).to_csv(p, index=False)
            paths["summary"] = str(p)

        sym_df = self.analytics.per_symbol_breakdown()
        if not sym_df.empty:
            p = self.output_dir / f"per_symbol{self._label}_{self._ts}.csv"
            sym_df.to_csv(p)
            paths["per_symbol"] = str(p)

        if self.analytics.equity_curve:
            p = self.output_dir / f"equity{self._label}_{self._ts}.csv"
            pd.DataFrame(self.analytics.equity_curve).to_csv(p, index=False)
            paths["equity"] = str(p)

        txt_path = self.export_text_summary()
        if txt_path:
            paths["text_summary"] = txt_path

        for name, path in paths.items():
            logger.info(f"[Journal] {name} → {path}")

        return paths

    def export_text_summary(self) -> Optional[str]:
        metrics = self.analytics.compute()
        if "error" in metrics:
            return None

        lines = [
            "=" * 47,
            "  PAPER TRADING SUMMARY REPORT",
            f"  Run    : {self._label.strip('_') or 'unnamed'}",
            f"  Time   : {self._ts}",
            "=" * 47,
            f"  {'Total Trades':<22}: {metrics['total_trades']}",
            f"  {'Winners':<22}: {metrics['winners']}",
            f"  {'Losers':<22}: {metrics['losers']}",
            f"  {'Win Rate':<22}: {metrics['win_rate_pct']}%",
            f"  {'Profit Factor':<22}: {metrics['profit_factor']}",
            f"  {'Expectancy':<22}: ${metrics['expectancy']}",
            f"  {'Total Net PnL':<22}: ${metrics['total_net_pnl']:+}",
            f"  {'Gross Profit':<22}: ${metrics['gross_profit']}",
            f"  {'Gross Loss':<22}: ${metrics['gross_loss']}",
            f"  {'Max Drawdown':<22}: ${metrics['max_drawdown']}",
            f"  {'Max Drawdown %':<22}: {metrics['max_drawdown_pct']}%",
            f"  {'Sharpe Ratio':<22}: {metrics['sharpe_ratio']}",
            f"  {'Return %':<22}: {metrics['return_pct']:+}%",
            f"  {'Initial Balance':<22}: ${metrics['initial_balance']}",
            f"  {'Final Balance':<22}: ${metrics['final_balance']}",
            f"  {'Avg Trade Duration':<22}: {metrics['avg_trade_duration_min']} min",
            "=" * 47,
        ]

        sym_df = self.analytics.per_symbol_breakdown()
        if not sym_df.empty:
            lines += [
                "",
                "  PER SYMBOL BREAKDOWN",
                "-" * 47,
                f"  {'Symbol':<10} {'Trades':>6} {'Win%':>6} {'Net PnL':>10} {'Avg PnL':>8}",
                "-" * 47,
            ]
            for sym, row in sym_df.iterrows():
                lines.append(
                    f"  {sym:<10} {int(row['trades']):>6} "
                    f"{row['win_rate_%']:>5}% "
                    f"${row['net_pnl']:>9} "
                    f"${row['avg_pnl']:>7}"
                )
            lines.append("=" * 47)

        p = self.output_dir / f"summary_report{self._label}_{self._ts}.txt"
        p.write_text("\n".join(lines))
        return str(p)

    def append_to_master(self, master_path: str = "./results/master_runs.csv") -> str:
        metrics = self.analytics.compute()
        if "error" in metrics:
            return master_path
        metrics["run_label"]   = self._label.strip("_")
        metrics["exported_at"] = self._ts
        df     = pd.DataFrame([metrics])
        master = Path(master_path)
        master.parent.mkdir(parents=True, exist_ok=True)
        if master.exists():
            df.to_csv(master, mode="a", header=False, index=False)
        else:
            df.to_csv(master, mode="w", header=True,  index=False)
        logger.info(f"[Journal] Appended to master → {master_path}")
        return master_path
