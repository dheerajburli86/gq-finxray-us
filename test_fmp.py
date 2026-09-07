"""
test_fmp.py
Replaces test_eodhd.py / test_eodhd_deep.py / test_eodhd_verify.py.

Quick smoke test for the FMP client — run this after dropping FMP_API_KEY
into .env to confirm the key works and the endpoints this codebase depends
on are reachable before trusting any poller output.
"""
import json
import fmp_client


def check(label, result):
    ok = bool(result)
    print(f"[{'OK' if ok else 'EMPTY/FAIL'}] {label}")
    return ok


def main():
    print("=" * 60)
    print("FMP client smoke test — AAPL")
    print("=" * 60)

    results = {}
    results["quote"] = fmp_client.get_quote("AAPL")
    check("get_quote(AAPL)", results["quote"])

    results["profile"] = fmp_client.get_profile("AAPL")
    check("get_profile(AAPL)", results["profile"])

    results["news"] = fmp_client.get_stock_news("AAPL", limit=3)
    check("get_stock_news(AAPL)", results["news"])

    results["income"] = fmp_client.get_income_statement("AAPL", period="quarter", limit=2)
    check("get_income_statement(AAPL)", results["income"])

    results["insider"] = fmp_client.get_insider_trading("AAPL", limit=5)
    check("get_insider_trading(AAPL)", results["insider"])

    results["rsi"] = fmp_client.get_rsi("AAPL")
    check("get_rsi(AAPL)", results["rsi"])

    results["transcript_dates"] = fmp_client.get_transcript_dates("AAPL")
    check("get_transcript_dates(AAPL)", results["transcript_dates"])

    results["commodity"] = fmp_client.get_commodity_quote("GCUSD")
    check("get_commodity_quote(GCUSD -- gold)", results["commodity"])

    print("\nSample quote payload:")
    print(json.dumps(results["quote"], indent=2, default=str)[:1000])


if __name__ == "__main__":
    main()
