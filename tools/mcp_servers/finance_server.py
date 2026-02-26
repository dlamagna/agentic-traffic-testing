"""
MCP server exposing simple, synthetic finance tools.

All data is fake and deterministic, intended purely for
traffic and behavior experiments in the testbed.
"""

from __future__ import annotations

from datetime import datetime
import random

from fastmcp import FastMCP


server = FastMCP("finance-server")


_STOCK_DATA = {
    "AAPL": {"price": 175.50, "change": 2.3},
    "GOOGL": {"price": 142.80, "change": -1.2},
    "MSFT": {"price": 378.90, "change": 3.5},
    "TSLA": {"price": 245.60, "change": -5.2},
}


@server.tool()
def get_stock_price(symbol: str) -> dict:
    """
    Return a synthetic current price for a stock symbol.

    The numbers are intentionally simple and not real market data.
    """
    symbol = symbol.upper()
    meta = _STOCK_DATA.get(symbol)
    if not meta:
        return {
            "error": f"Unknown symbol: {symbol}",
            "available_symbols": sorted(_STOCK_DATA.keys()),
        }

    # Add a small random perturbation so repeated calls aren't identical.
    price = meta["price"] + random.uniform(-5, 5)
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "change_percent": round(meta["change"], 2),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@server.tool()
def calculate_portfolio_value(holdings: dict[str, float]) -> dict:
    """
    Compute portfolio value using the synthetic prices above.

    Args:
        holdings: mapping of stock symbol → share count.
    """
    total_value = 0.0
    positions = []

    for symbol, shares in holdings.items():
        sym = symbol.upper()
        meta = _STOCK_DATA.get(sym)
        if not meta:
            continue
        price = meta["price"]
        value = price * float(shares)
        total_value += value
        positions.append(
            {
                "symbol": sym,
                "shares": float(shares),
                "price": round(price, 2),
                "value": round(value, 2),
            }
        )

    return {
        "total_value": round(total_value, 2),
        "positions": positions,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@server.resource("resource://market/indices")
def list_indices() -> dict:
    """Return a tiny synthetic snapshot of market indices."""
    now = datetime.utcnow().isoformat() + "Z"
    return {
        "indices": {
            "S&P 500": 4567.89,
            "Dow Jones": 35432.10,
            "NASDAQ": 14234.56,
        },
        "updated": now,
    }


if __name__ == "__main__":
    server.run()

