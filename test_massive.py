"""
test_massive.py
Replaces test_td_crypto.py / test_td_crypto_v2.py / twelvedata_crypto_test.txt.

Quick smoke test for the Massive client (formerly Polygon.io) — covers
stocks (snapshot, RSI, SMA) and crypto (snapshot), since Massive is now
the crypto data source instead of TwelveData.
"""
import json
import massive_client


def check(label, result):
    ok = bool(result)
    print(f"[{'OK' if ok else 'EMPTY/FAIL'}] {label}")
    return ok


def main():
    print("=" * 60)
    print("Massive client smoke test")
    print("=" * 60)

    snap = massive_client.get_snapshot("AAPL")
    check("get_snapshot(AAPL)", snap)

    rsi = massive_client.get_rsi("AAPL")
    check("get_rsi(AAPL)", rsi)

    sma = massive_client.get_sma("AAPL", window=200)
    check("get_sma(AAPL, 200)", sma)

    news = massive_client.get_news(ticker="AAPL", limit=3)
    check("get_news(AAPL)", news)

    # Crypto — replaces the old TwelveData crypto test scripts
    btc = massive_client.get_crypto_snapshot("X:BTCUSD")
    check("get_crypto_snapshot(X:BTCUSD)", btc)

    print("\nSample AAPL snapshot payload:")
    print(json.dumps(snap, indent=2, default=str)[:1000])


if __name__ == "__main__":
    main()
