"""
connection_tester.py
====================
Quick MT5 connection test. Run this first to verify your setup.

Usage:
    python connection_tester.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5

MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

print("Initializing...")
result = mt5.initialize(path=MT5_PATH, timeout=10_000)
print(f"Result : {result}")
print(f"Error  : {mt5.last_error()}")

if result:
    info = mt5.terminal_info()
    print(f"Terminal      : {info.name}")
    print(f"Build         : {info.build}")
    print(f"Connected     : {info.connected}")
    print(f"Trade allowed : {info.trade_allowed}")
    print(f"Path          : {info.path}")

    # Show available symbols containing XAU or GOLD
    symbols = mt5.symbols_get()
    gold    = [s.name for s in symbols if "XAU" in s.name or "GOLD" in s.name.upper()]
    print(f"\nGold symbols  : {gold}")

    mt5.shutdown()
    print("\n✅ Connection OK — update MT5_PATH in your config if needed.")
else:
    print("\n❌ Connection failed.")
    print("   1. Open MetaTrader 5 terminal manually")
    print("   2. Enable Algo Trading (green button in toolbar)")
    print("   3. Update MT5_PATH in this file to your terminal64.exe path")
