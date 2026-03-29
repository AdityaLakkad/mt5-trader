//+------------------------------------------------------------------+
//|                                   BB_Engulfing_Breakout.mq5     |
//|                    Bollinger Band Engulfing Breakout Strategy    |
//|                                                                  |
//| HOW TO USE:                                                      |
//|   1. Open MetaEditor (F4 in MT5)                                 |
//|   2. File → New → Expert Advisor                                 |
//|   3. Name it BB_Engulfing_Breakout                               |
//|   4. Delete template code, paste this file                       |
//|   5. Press F7 to compile — should show 0 errors                  |
//|   6. Drag EA onto XAUUSD.GNE M15 chart                           |
//|   7. Enable "Allow live trading" in EA settings                  |
//|                                                                  |
//| PAPER TRADING:                                                   |
//|   Run on a DEMO account — identical to live but no real money    |
//|                                                                  |
//| BACKTESTING:                                                     |
//|   View → Strategy Tester → select this EA → press Start          |
//|   Check "Visual mode" to watch trades being placed live          |
//+------------------------------------------------------------------+

#property copyright "MT5 Paper Trading Framework"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| INPUTS                                                           |
//+------------------------------------------------------------------+

input group "=== Bollinger Bands ==="
input int    InpBBPeriod   = 20;         // BB Period
input double InpBBStdDev   = 2.0;        // BB Standard Deviation

input group "=== Engulfing Filter ==="
input double InpTolerance  = 10.0;       // Tolerance % (expands body each side)

input group "=== Signal Settings ==="
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_M15;  // Timeframe
input int    InpExpiry     = 5;          // Expiry candles (0 = never expire)

input group "=== Position Sizing ==="
input string InpSizingMode = "FIXED_LOTS";  // FIXED_LOTS | FIXED_USD | RISK_PCT
input double InpFixedLots  = 0.1;        // Lots (FIXED_LOTS mode)
input double InpRiskUSD    = 100.0;      // Risk $ per trade (FIXED_USD mode)
input double InpRiskPct    = 1.0;        // Risk % of balance (RISK_PCT mode)
input double InpMaxLots    = 10.0;       // Maximum lot size (all modes)
input double InpMinLots    = 0.01;       // Minimum lot size (all modes)

input group "=== TP / SL ==="
input string InpTPSLMode   = "POINTS";  // POINTS | PERCENT
input double InpTPPoints   = 40.0;      // TP in points (POINTS mode)
input double InpSLPoints   = 20.0;      // SL in points (POINTS mode)
input double InpTPPct      = 2.0;       // TP % above entry (PERCENT mode)
input double InpSLPct      = 1.0;       // SL % below entry (PERCENT mode)

input group "=== General ==="
input int    InpMagic      = 234001;    // Magic number
input int    InpSlippage   = 10;        // Max slippage (points)


//+------------------------------------------------------------------+
//| ENUMS & STRUCTS                                                  |
//+------------------------------------------------------------------+

enum StrategyState { STATE_IDLE, STATE_WAITING, STATE_IN_TRADE };

struct PendingSignal
{
    string   direction;        // "long" or "short"
    double   breakout_high;
    double   breakout_low;
    datetime signal_time;
    int      candles_elapsed;
};

//+------------------------------------------------------------------+
//| GLOBALS                                                          |
//+------------------------------------------------------------------+

StrategyState g_state    = STATE_IDLE;
PendingSignal g_pending;
CTrade        g_trade;
int           g_bb_handle = INVALID_HANDLE;
datetime      g_last_bar  = 0;


//+------------------------------------------------------------------+
//| HELPER: Engulfing check                                          |
//+------------------------------------------------------------------+
string IsEngulfing(double curr_open, double curr_close,
                   double prev_open, double prev_close,
                   double tolerance_pct)
{
    double curr_body     = MathAbs(curr_close - curr_open);
    double prev_body     = MathAbs(prev_close - prev_open);

    if(prev_body == 0.0) return "none";

    double tol_amount    = curr_body * (tolerance_pct / 100.0);
    double expanded_body = curr_body + (tol_amount * 2.0);

    if(expanded_body <= prev_body) return "none";

    if(curr_close > curr_open) return "bullish";
    if(curr_close < curr_open) return "bearish";
    return "none";
}


//+------------------------------------------------------------------+
//| HELPER: BB touch checks                                          |
//+------------------------------------------------------------------+
bool TouchesLowerBB(double open, double close, double lower)
{
    return (open < lower && close > lower);
}

bool TouchesUpperBB(double open, double close, double upper)
{
    return (open > upper && close < upper);
}


//+------------------------------------------------------------------+
//| HELPER: Has open position for this EA                            |
//+------------------------------------------------------------------+
bool HasOpenPosition()
{
    for(int i = 0; i < PositionsTotal(); i++)
    {
        ulong ticket = PositionGetTicket(i);
        if(PositionSelectByTicket(ticket))
        {
            if(PositionGetInteger(POSITION_MAGIC) == InpMagic &&
               PositionGetString(POSITION_SYMBOL) == _Symbol)
                return true;
        }
    }
    return false;
}


//+------------------------------------------------------------------+
//| HELPER: Calculate lot size                                       |
//+------------------------------------------------------------------+
double CalculateLotSize(double entry_price)
{
    double raw_lots = InpFixedLots;

    if(InpSizingMode == "FIXED_USD" || InpSizingMode == "RISK_PCT")
    {
        double point         = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
        double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
        double sl_val_lot    = InpSLPoints * point * contract_size;

        if(sl_val_lot <= 0.0)
        {
            Print("⚠️ SL value per lot = 0, using min lots");
            return InpMinLots;
        }

        double risk_amount;
        if(InpSizingMode == "FIXED_USD")
        {
            risk_amount = InpRiskUSD;
        }
        else  // RISK_PCT
        {
            double balance = AccountInfoDouble(ACCOUNT_BALANCE);
            risk_amount    = balance * (InpRiskPct / 100.0);
        }

        raw_lots = risk_amount / sl_val_lot;
    }

    // Snap to volume step
    double vol_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double vol_min  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double vol_max  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

    double snapped = MathRound(raw_lots / vol_step) * vol_step;
    double clamped = MathMax(InpMinLots, MathMin(InpMaxLots, snapped));
    return MathMax(vol_min, MathMin(vol_max, clamped));
}


//+------------------------------------------------------------------+
//| HELPER: Calculate TP and SL prices                               |
//+------------------------------------------------------------------+
void CalcTPSL(string direction, double entry,
              double &tp_out, double &sl_out)
{
    double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

    if(InpTPSLMode == "POINTS")
    {
        if(direction == "long")
        {
            tp_out = NormalizeDouble(entry + InpTPPoints * point, digits);
            sl_out = NormalizeDouble(entry - InpSLPoints * point, digits);
        }
        else
        {
            tp_out = NormalizeDouble(entry - InpTPPoints * point, digits);
            sl_out = NormalizeDouble(entry + InpSLPoints * point, digits);
        }
    }
    else  // PERCENT
    {
        if(direction == "long")
        {
            tp_out = NormalizeDouble(entry * (1.0 + InpTPPct / 100.0), digits);
            sl_out = NormalizeDouble(entry * (1.0 - InpSLPct / 100.0), digits);
        }
        else
        {
            tp_out = NormalizeDouble(entry * (1.0 - InpTPPct / 100.0), digits);
            sl_out = NormalizeDouble(entry * (1.0 + InpSLPct / 100.0), digits);
        }
    }
}


//+------------------------------------------------------------------+
//| HELPER: Reset state                                              |
//+------------------------------------------------------------------+
void ResetState()
{
    g_state                   = STATE_IDLE;
    g_pending.direction       = "";
    g_pending.breakout_high   = 0.0;
    g_pending.breakout_low    = 0.0;
    g_pending.signal_time     = 0;
    g_pending.candles_elapsed = 0;
}


//+------------------------------------------------------------------+
//| EA INIT                                                          |
//+------------------------------------------------------------------+
int OnInit()
{
    g_bb_handle = iBands(_Symbol, InpTimeframe, InpBBPeriod, 0, InpBBStdDev, PRICE_CLOSE);

    if(g_bb_handle == INVALID_HANDLE)
    {
        PrintFormat("❌ Failed to create BB indicator: %d", GetLastError());
        return INIT_FAILED;
    }

    g_trade.SetExpertMagicNumber(InpMagic);
    g_trade.SetDeviationInPoints(InpSlippage);

    PrintFormat("✅ BB Engulfing Breakout EA v2.00 started");
    PrintFormat("   Symbol     : %s", _Symbol);
    PrintFormat("   Timeframe  : %s", EnumToString(InpTimeframe));
    PrintFormat("   BB         : %d / %.1f", InpBBPeriod, InpBBStdDev);
    PrintFormat("   Tolerance  : %.1f%%", InpTolerance);
    PrintFormat("   Expiry     : %d candles", InpExpiry);
    PrintFormat("   Sizing     : %s", InpSizingMode);
    PrintFormat("   TP/SL      : %s  TP=%.1f  SL=%.1f",
                InpTPSLMode, InpTPPoints, InpSLPoints);

    ResetState();
    return INIT_SUCCEEDED;
}


//+------------------------------------------------------------------+
//| EA DEINIT                                                        |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    if(g_bb_handle != INVALID_HANDLE)
        IndicatorRelease(g_bb_handle);
    PrintFormat("EA stopped. Reason code: %d", reason);
}


//+------------------------------------------------------------------+
//| MAIN TICK                                                        |
//+------------------------------------------------------------------+
void OnTick()
{
    // ── Check if our position was closed (TP/SL hit by broker) ────
    if(g_state == STATE_IN_TRADE && !HasOpenPosition())
    {
        Print("📋 Position closed by broker (TP/SL) → IDLE");
        ResetState();
    }

    // ── Breakout check on every tick ──────────────────────────────
    if(g_state == STATE_WAITING)
    {
        double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

        if(g_pending.direction == "long" && ask >= g_pending.breakout_high)
        {
            OpenLong(ask);
            return;
        }
        if(g_pending.direction == "short" && bid <= g_pending.breakout_low)
        {
            OpenShort(bid);
            return;
        }
    }

    // ── Bar close detection ────────────────────────────────────────
    datetime current_bar = iTime(_Symbol, InpTimeframe, 0);
    if(current_bar == g_last_bar) return;
    g_last_bar = current_bar;

    OnBarClose();
}


//+------------------------------------------------------------------+
//| BAR CLOSE LOGIC                                                  |
//+------------------------------------------------------------------+
void OnBarClose()
{
    // ── Expiry counter ─────────────────────────────────────────────
    if(g_state == STATE_WAITING)
    {
        g_pending.candles_elapsed++;
        if(InpExpiry > 0 && g_pending.candles_elapsed >= InpExpiry)
        {
            PrintFormat("⏰ Signal EXPIRED after %d candles → IDLE", InpExpiry);
            ResetState();
        }
    }

    if(g_state == STATE_IN_TRADE) return;

    // ── Fetch BB values ────────────────────────────────────────────
    // Buffer 0 = middle, 1 = upper, 2 = lower (MT5 convention)
    // Index 1 = last CLOSED bar
    double upper[1], lower[1], middle[1];
    if(CopyBuffer(g_bb_handle, 1, 1, 1, upper)  < 1 ||
       CopyBuffer(g_bb_handle, 2, 1, 1, lower)  < 1 ||
       CopyBuffer(g_bb_handle, 0, 1, 1, middle) < 1)
    {
        Print("⚠️ BB values unavailable");
        return;
    }

    // ── Fetch candle data ──────────────────────────────────────────
    // [1] = last closed bar = "current candle"
    // [2] = bar before that = "previous candle"
    double curr_open  = iOpen (_Symbol, InpTimeframe, 1);
    double curr_close = iClose(_Symbol, InpTimeframe, 1);
    double curr_high  = iHigh (_Symbol, InpTimeframe, 1);
    double curr_low   = iLow  (_Symbol, InpTimeframe, 1);
    double prev_open  = iOpen (_Symbol, InpTimeframe, 2);
    double prev_close = iClose(_Symbol, InpTimeframe, 2);

    // ── Engulfing check ────────────────────────────────────────────
    string engulf = IsEngulfing(curr_open, curr_close,
                                prev_open, prev_close,
                                InpTolerance);
    if(engulf == "none") return;

    // ── BB touch + signal ──────────────────────────────────────────
    if(engulf == "bullish" && TouchesLowerBB(curr_open, curr_close, lower[0]))
    {
        g_state                   = STATE_WAITING;
        g_pending.direction       = "long";
        g_pending.breakout_high   = curr_high;
        g_pending.breakout_low    = curr_low;
        g_pending.signal_time     = TimeCurrent();
        g_pending.candles_elapsed = 0;

        PrintFormat("🟢 LONG SETUP | H=%.5f L=%.5f | lower_bb=%.5f | expires in %d bars",
                    curr_high, curr_low, lower[0], InpExpiry);
    }
    else if(engulf == "bearish" && TouchesUpperBB(curr_open, curr_close, upper[0]))
    {
        g_state                   = STATE_WAITING;
        g_pending.direction       = "short";
        g_pending.breakout_high   = curr_high;
        g_pending.breakout_low    = curr_low;
        g_pending.signal_time     = TimeCurrent();
        g_pending.candles_elapsed = 0;

        PrintFormat("🔴 SHORT SETUP | H=%.5f L=%.5f | upper_bb=%.5f | expires in %d bars",
                    curr_high, curr_low, upper[0], InpExpiry);
    }
}


//+------------------------------------------------------------------+
//| OPEN LONG                                                        |
//+------------------------------------------------------------------+
void OpenLong(double ask)
{
    double tp, sl;
    CalcTPSL("long", ask, tp, sl);
    double lots = CalculateLotSize(ask);

    PrintFormat("✅ LONG BREAKOUT | ask=%.5f >= level=%.5f | TP=%.5f SL=%.5f lots=%.2f",
                ask, g_pending.breakout_high, tp, sl, lots);

    g_state = STATE_IN_TRADE;   // set BEFORE order to block duplicate ticks

    if(!g_trade.Buy(lots, _Symbol, ask, sl, tp, "BB_Engulf_Long"))
    {
        PrintFormat("❌ Buy FAILED: %d %s → IDLE",
                    g_trade.ResultRetcode(),
                    g_trade.ResultRetcodeDescription());
        ResetState();
    }
}


//+------------------------------------------------------------------+
//| OPEN SHORT                                                       |
//+------------------------------------------------------------------+
void OpenShort(double bid)
{
    double tp, sl;
    CalcTPSL("short", bid, tp, sl);
    double lots = CalculateLotSize(bid);

    PrintFormat("✅ SHORT BREAKOUT | bid=%.5f <= level=%.5f | TP=%.5f SL=%.5f lots=%.2f",
                bid, g_pending.breakout_low, tp, sl, lots);

    g_state = STATE_IN_TRADE;

    if(!g_trade.Sell(lots, _Symbol, bid, sl, tp, "BB_Engulf_Short"))
    {
        PrintFormat("❌ Sell FAILED: %d %s → IDLE",
                    g_trade.ResultRetcode(),
                    g_trade.ResultRetcodeDescription());
        ResetState();
    }
}
