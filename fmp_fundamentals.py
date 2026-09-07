"""
fmp_fundamentals.py
Replaces eodhd_fundamentals.py — CLI fundamentals viewer, now on FMP.

FMP splits what EODHD gave you in one giant nested payload across several
flat endpoints (profile, key-metrics, income-statement, balance-sheet-
statement, cashflow-statement) — this pulls each and prints the same
sectioned report as before.
"""
import json
import fmp_client

TICKER = "AAPL"


def fmt(label, value, width=45):
    print(f"  {label:{width}s}: {value}")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def display(ticker):
    profile = fmp_client.get_profile(ticker) or {}
    key_metrics = fmp_client.get_key_metrics(ticker, period="quarter", limit=1)
    km = key_metrics[0] if key_metrics else {}
    quote = fmp_client.get_quote(ticker) or {}

    print("\n" + "=" * 60)
    print(f"  {profile.get('companyName')} ({profile.get('symbol')}.{profile.get('exchangeShortName', profile.get('exchange', ''))})")
    print(f"  {profile.get('sector')} -> {profile.get('industry')}")
    employees = profile.get("fullTimeEmployees")
    print(f"  Employees: {employees or 'N/A'}  |  IPO: {profile.get('ipoDate')}")
    print(f"  Currency: {profile.get('currency')}  |  Country: {profile.get('country')}")
    print("=" * 60)

    section("HIGHLIGHTS")
    highlight_keys = [
        ("mktCap", "Market Cap"),
        ("beta", "Beta"),
        ("price", "Price"),
        ("lastDividend", "Last Dividend"),
        ("range", "52-Week Range"),
    ]
    for key, label in highlight_keys:
        val = profile.get(key)
        if val is not None:
            fmt(label, val)

    section("KEY METRICS (latest quarter)")
    km_keys = [
        ("peRatio", "P/E Ratio"),
        ("pbRatio", "P/B Ratio"),
        ("evToEbitda", "EV / EBITDA"),
        ("freeCashFlowPerShare", "FCF per Share"),
        ("returnOnEquity", "Return on Equity"),
        ("returnOnAssets", "Return on Assets"),
        ("netDebtToEBITDA", "Net Debt / EBITDA"),
    ]
    for key, label in km_keys:
        val = km.get(key)
        if val is not None:
            fmt(label, val)

    section("LIVE QUOTE")
    q_keys = [
        ("price", "Price"), ("change", "Change"), ("changePercentage", "Change %"),
        ("dayLow", "Day Low"), ("dayHigh", "Day High"),
        ("yearLow", "52-Week Low"), ("yearHigh", "52-Week High"),
        ("volume", "Volume"), ("avgVolume", "Avg Volume"),
    ]
    for key, label in q_keys:
        val = quote.get(key)
        if val is not None:
            fmt(label, val)

    income = fmp_client.get_income_statement(ticker, period="annual", limit=1)
    if income:
        row = income[0]
        section(f"INCOME STATEMENT -- FY {row.get('fiscalYear', row.get('date', ''))}")
        is_keys = [
            ("revenue", "Total Revenue"), ("costOfRevenue", "Cost of Revenue"),
            ("grossProfit", "Gross Profit"), ("researchAndDevelopmentExpenses", "R&D Expense"),
            ("sellingGeneralAndAdministrativeExpenses", "SG&A"),
            ("operatingIncome", "Operating Income"), ("ebitda", "EBITDA"),
            ("netIncome", "Net Income"), ("epsDiluted", "Diluted EPS"),
        ]
        for key, label in is_keys:
            val = row.get(key)
            if val is not None:
                fmt(label, val)

    balance = fmp_client.get_balance_sheet(ticker, period="annual", limit=1)
    if balance:
        row = balance[0]
        section(f"BALANCE SHEET -- FY {row.get('fiscalYear', row.get('date', ''))}")
        bs_keys = [
            ("totalAssets", "Total Assets"), ("totalLiabilities", "Total Liabilities"),
            ("totalStockholdersEquity", "Stockholder Equity"),
            ("cashAndCashEquivalents", "Cash & Equivalents"),
            ("shortTermInvestments", "Short-Term Investments"),
            ("netReceivables", "Net Receivables"),
            ("totalCurrentAssets", "Total Current Assets"),
            ("totalCurrentLiabilities", "Total Current Liabilities"),
            ("longTermDebt", "Long-Term Debt"),
        ]
        for key, label in bs_keys:
            val = row.get(key)
            if val is not None:
                fmt(label, val)

    cashflow = fmp_client.get_cash_flow(ticker, period="annual", limit=1)
    if cashflow:
        row = cashflow[0]
        section(f"CASH FLOW -- FY {row.get('fiscalYear', row.get('date', ''))}")
        cf_keys = [
            ("operatingCashFlow", "Operating Cash Flow"),
            ("capitalExpenditure", "Capital Expenditures"),
            ("freeCashFlow", "Free Cash Flow"),
            ("netCashUsedForInvestingActivites", "Investing Cash Flow"),
            ("netCashUsedProvidedByFinancingActivities", "Financing Cash Flow"),
            ("dividendsPaid", "Dividends Paid"),
            ("commonStockRepurchased", "Stock Buybacks"),
        ]
        for key, label in cf_keys:
            val = row.get(key)
            if val is not None:
                fmt(label, val)

    print("\n" + "=" * 60 + "\n")

    return {"profile": profile, "key_metrics": km, "quote": quote, "income": income,
            "balance": balance, "cashflow": cashflow}


if __name__ == "__main__":
    print(f"\nFetching fundamentals for {TICKER} from FMP...")
    data = display(TICKER)
    out_file = f"{TICKER}_fundamentals_fmp.json"
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Raw JSON saved to: {out_file}\n")
