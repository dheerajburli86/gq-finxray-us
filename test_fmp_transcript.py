#!/usr/bin/env python3
"""
Diagnostic script to test FMP API key and earnings transcript endpoint.
Run this to verify your FMP API key is valid and has Ultimate tier access.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

import fmp_client

def test_api_key():
    """Test if FMP API key is set and valid."""
    key = os.getenv("FMP_API_KEY")

    print("=" * 70)
    print("FMP API KEY DIAGNOSTIC TEST")
    print("=" * 70)

    if not key:
        print("❌ ERROR: FMP_API_KEY not set in .env file")
        print("\nFix: Add this to your .env file:")
        print("   FMP_API_KEY=your_api_key_here")
        return False

    if len(key) < 10:
        print(f"⚠️  WARNING: FMP_API_KEY looks too short ({len(key)} chars)")
        return False

    print(f"✅ FMP_API_KEY is set ({len(key)} characters)")
    return True


def test_basic_endpoint():
    """Test basic FMP endpoint (quote) to verify API key works."""
    print("\n" + "-" * 70)
    print("Testing basic endpoint: /quote (AAPL)")
    print("-" * 70)

    try:
        result = fmp_client.get_quote("AAPL")
        if result:
            print(f"✅ Quote endpoint works")
            print(f"   AAPL price: ${result.get('price', 'N/A')}")
            return True
        else:
            print("❌ Quote endpoint returned no data")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def test_transcript_dates():
    """Test transcript dates endpoint."""
    print("\n" + "-" * 70)
    print("Testing transcript dates endpoint: /transcripts-dates-by-symbol (AAPL)")
    print("-" * 70)

    try:
        result = fmp_client.get_transcript_dates("AAPL")
        if result and isinstance(result, list):
            print(f"✅ Transcript dates endpoint works")
            print(f"   Found {len(result)} transcript dates for AAPL:")
            for entry in result[:3]:
                year = entry.get("fiscalYear") or entry.get("year")
                quarter = entry.get("period") or entry.get("quarter")
                date = entry.get("date")
                print(f"     - FY{year} Q{quarter} ({date})")
            if len(result) > 3:
                print(f"     ... and {len(result) - 3} more")
            return True
        else:
            print("⚠️  No transcripts found for AAPL (might be normal)")
            return True  # Not an error, just no data
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def test_transcript_fetch():
    """Test fetching an actual transcript."""
    print("\n" + "-" * 70)
    print("Testing transcript fetch: /earning-call-transcript (AAPL 2024 Q4)")
    print("-" * 70)

    try:
        result = fmp_client.get_earnings_transcript("AAPL", 2024, 4)
        if result and isinstance(result, dict):
            content = result.get("content", "")
            print(f"✅ Transcript fetch works")
            print(f"   Content length: {len(content)} characters")
            if len(content) > 0:
                print(f"   First 100 chars: {content[:100]}...")
            return True
        else:
            print("⚠️  No transcript data returned for AAPL 2024 Q4")
            print("   This might be normal if transcript not yet available")
            return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Run all diagnostic tests."""
    tests = [
        ("API Key Check", test_api_key),
        ("Basic Endpoint", test_basic_endpoint),
        ("Transcript Dates", test_transcript_dates),
        ("Transcript Fetch", test_transcript_fetch),
    ]

    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n❌ Unexpected error in {name}: {e}")
            results.append((name, False))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status:10} {name}")

    print(f"\nTotal: {passed_count}/{total_count} tests passed")

    if passed_count == total_count:
        print("\n🎉 All tests passed! Your FMP API key is working correctly.")
        print("Earnings transcript alerts should now work properly.")
        return 0
    elif passed_count >= 2:
        print("\n⚠️  Some tests failed. Check the errors above.")
        print("Most likely: API key invalid or lacks Ultimate tier access.")
        return 1
    else:
        print("\n❌ Critical failures detected. Fix the errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
